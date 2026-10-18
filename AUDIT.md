# 代码审计与防御性加固报告

审计范围：全部 15 个 Python 模块 + 1 个单文件前端（`assets/index.html`，2200 行）。
审计方式：4 个独立 agent 分模块通读 + 本机最小复现验证；raw 清单见 `data/audit/*.json`。

**结论：找到 121 个问题（critical 10 / major 35 / minor 76），两轮共修 54 个 —— 10 个 critical 全修，major 修掉 44 个。**
剩下的 67 条都是「不修也不会出事」的健壮性/性能项，见文末清单。

---

## 一、最要命的三类问题

### 1. 退出路径不还原系统代理 → 用户断网

这是最严重的：只要有一条路径没做清理，注册表就永久指向一个已经退出的 `127.0.0.1:7891`，
浏览器、微信、Steam 全部连不上，而且重启程序也不一定能自己修好。

修掉的 5 个漏口：

| 位置 | 原来的问题 | 现在 |
|---|---|---|
| `run.pyw:98` | `finally` 只调 `webui.stop()`，托盘 `run()` 任何非菜单返回都留下 mihomo 孤儿 + 代理残留 | 先 `app.shutdown()` 再停面板 |
| `tray_win.py:466` | 窗口过程没有 `WM_QUERYENDSESSION`，**关机/注销时清理完全不执行** | 收到就同步还原代理再放行 |
| `kernel.py:133` | 没有任何 `atexit`，父进程被任务管理器结束即成孤儿 | 注册 `atexit` 兜底 kill |
| `sysproxy.py:136` | `disable()` 里任一次 `_write` 抛错就整体中断，可能停在「ProxyEnable=1 但 ProxyServer 已删」 | 每步独立兜底，失败超 1 项就强制关代理 |
| `app.py:1032` | 先改注册表再存 settings，save 抛异常则退出时按旧值判断，漏还原 | 先落盘再动注册表，失败双向回滚 |

### 2. 前端一个字段缺失就整页白屏

`render()` 里 `s.ports.proxy`、`s.sub.url`、`s.info.length` 全是裸访问，而它被 `poll()` 的
`catch {}` 静默吞掉 —— 表现就是「每 1.5 秒抛一次错、页面永远停在旧帧」，首帧崩则整页空白。
另外顶层 `new Set(JSON.parse(localStorage...))` 在隐私模式下抛 `SecurityError`，
这一行在函数定义之前，抛出后整个脚本不执行。

现在：`render()` 入口补齐 `ports/sub/info/nodes/traffic/progress` 默认值；
`ccStats/redrawPins/renderNodes/showCluster/renderAI/renderSubHead/renderSubs` 全部加 `STATE` 守卫；
`localStorage` 读取包 try。`tools/ui_smoke.py` 跑下来 **0 个 JS 报错**。

### 3. 检测任务能被双击并发

`check_speed` 与 `measure_purity` 都是「先查 flag，再置位，中间隔了几十行」。
面板上双击两下就会跑两份，两份互相抢节点，最终把出口 IP / 速度**写到错的节点上**（数据污染，不是崩溃，更难发现）。
现在统一走 `self._claim_task()`（`threading.Lock` + 置位原子完成）。

---

## 二、已修复清单（54 条）

### 第二轮（v1.5）20 条

**性能 · 节点列表改增量渲染（最大的一个）**
原来每 1.5 秒把 100 行节点整表 `innerHTML` 重建一遍 —— 你在列表上点选、滚动、或者刚好在复制 IP，
都会被重建打断；100 节点时单帧约 8ms，还伴随整块重排。现在改成按 signature 判断：
节点集合/顺序没变就只更新每行的延迟数字和颜色，不重建 DOM；顺序变了用
`insertBefore` 挪位置而不是重排；切国家、改过滤条件才真正重建，且重建后缓存会自动失效。
`tools/verify_incr.py` 实测：跨 3 轮轮询后首行 DOM 元素仍是同一个（标记存活），散点位置不再
每帧乱跳（原来 `Math.random()`，改用名字哈希），切国家往返后列表和编号点都能自愈。

**安全 · 补齐 `esc()` 覆盖缺口**
`nodeTip` / `nodeLocHtml` / 区域标题 / 聚簇弹层 / 订阅备注名 / 地图提示，原来有 7 处把后端或
用户输入的字符串直接拼进 `innerHTML`。备注名是用户自己填的、节点名来自订阅（远程内容），
现在全部转义。另修 `openLocal` 的 `d.d`（SVG 路径）未转义、`L.districts` 缺字段直接抛错。

**健壮 · 数值脏值不再让渲染崩**
`projectLL` 现在夹住 lat/lon 并拒绝非数值 —— 原来一个 `lat=91` 就能让 `millerY` 出 `NaN` 传染到
整张地图变空白；`hist.up/down` 用 `Number.isFinite` 归一，速度字段是字符串时曲线不再消失；
订阅剩余流量 `toFixed` 前先 `Number()` 归一（后端降级可能给 `"None"`）。

**交互 · 六个开关不再「假装成功」**
模式 / 系统代理 / 开机自启 / 自动优选 / 节点自愈 / AI 通道，原来点击后立刻按「已切换」重绘，
后端返回 `ok:false` 也照样变绿。现在失败会回滚到原状态并提示原因。

**托盘 · 菜单构建失败不再让右键彻底失灵**
`_build_node_menu` 里任何一个 `CreatePopupMenu`/`AppendMenu` 抛异常（节点名超长、HMENU 句柄耗尽），
原来会把半截菜单和已建的子菜单全部泄漏，且异常冒到消息循环后右键从此没反应 —— 连「退出」都点不到。
现在整段包 try/finally，失败销毁所有句柄、回落成只显示固定项，并每轮重建前 `_node_cmds.clear()`。

**托盘 · 节点名里的 `&` 不再变成下划线**
Win32 菜单把 `&` 当助记符前缀，订阅里带 `&` 的节点名会显示成「下一字符带下划线」。

**托盘 · 启动早期点退出不再丢请求**
`_quit` 原来在窗口句柄还没建好时 `PostMessage` 抛异常 → 直接 return，退出请求被吞（面板关了进程还在）。
现在句柄缺失时就在当前线程就地清理。

**进程 · 按端口杀进程前先验明身份**
`cli.py` 的 `down` 和 `kernel.py` 的端口清理原来只认端口号，照 `netstat` 找到谁就 `taskkill` ——
端口被你自己的别的代理、公司安全软件、开发时的临时服务占着时会被误杀。现在先 `tasklist` 查进程名，
只有 `mihomo*` 才动手；查不到名字（只剩残留内核）才退回按端口处理。

**busy 状态漏清的三条路径**
`test_delays`、`restart_kernel`、`use_subscription` 的 `busy` 只在正常路径清。中间抛一次异常，
`busy` 就永久卡住 —— `_wait_idle()` 从此永远返回 `False`，之后「测纯净度 / 带宽测速」全部被静默跳过，
面板上一点提示都没有。现在三处都包 `try/finally`。

**启动 · 日志文件打不开不再导致双击无反应**
`logger.setup()` 里 `RotatingFileHandler` 在文件被占用（拿记事本打开、上一实例没退干净）或目录只读时
直接抛异常，异常冒到 `main()` 外层被静默吞掉 —— 双击图标什么都不会发生。现在降级成 `NullHandler`
并记下 `SETUP_ERROR`，程序继续启动；滚动日志（rollover）失败也只影响这一轮，不会让日志永久停摆。

**启动 · 单实例判断改用 `Local\` 命名互斥体**
`Global\` 命名对象需要 `SeCreateGlobalPrivilege`，普通用户拿不到时 `CreateMutexW` 直接失败，
判断永远返回 `False` —— 两个实例都起来了，第二个会把第一个的内核顶掉。同时句柄留到进程退出
（原来创建完就丢了），`CreateMutexW` 失败时选择放行而不是拦死。

### 第一轮（v1.4）34 条

**kernel.py（5）**
- 看门狗自我复制：`start()` 无条件新建 `_watch`，而 `_restarts=0` 又让 6 次上限失效 → 统一走 `_ensure_watch()`，只有主动启动才清零计数
- 无 atexit 兜底 → `_atexit_kill()`
- 只校验 `mixed_port`，9091 被占时会连上别人的服务判成「就绪」→ 两个端口都校验
- `port_free` 带 `SO_REUSEADDR`，Windows 上对**正在监听**的端口 bind 会成功 → 先 `connect_ex` 探测再 bind
- `open(KERNEL_LOG)` 在 try 之外，日志被占用时异常逃出 `start()` → 移进 try
- 看门狗 `time.sleep` 不可中断 + `self.proc=None` 在锁外 → 改 `Event.wait()`，进程指针加锁比对

**sysproxy.py（4）**
- `disable()` 无兜底（见上表）
- `enable()` 无锁，与 20 秒健康看护并发时会把备份写成「全关」，用户真实代理设置永久丢失 → 加 `RLock`
- 备份非原子写 → tmp + fsync + `os.replace`
- `_load_backup` 里 `p is LEGACY_BACKUP` 的身份比较在 Windows `Path` 上不可靠 → 改 `==` 且不重复迁移

**tray_win.py（5）**
- 关机不还原（见上表）
- 气泡通知 100% 失效：把 6 元组拼成 9 元组丢给 pywin32，实测抛 `TypeError` 被吞 → 用 `ctypes` 建真正的 `NOTIFYICONDATAW`
- 首次 `NIM_ADD` 失败（开机自启时 explorer 未就绪）后图标永久不出现 → 失败不记 hicon、下一轮重试；并处理 `TaskbarCreated` 重注册
- `_quit` 无超时看门狗、可重入 → `_shutting` 标记 + 12 秒兜底 `os._exit`
- 图标失败路径泄漏 HICON → 失败即 `DestroyIcon`

**app.py（7）**
- 检测任务并发（见上）
- `remove_subscription` 无锁 + 删完用改过的下标做比较，可能删错 / 下标越界 → 全流程加锁，改用删除前的快照判断
- 系统代理顺序（见上）
- `refresh_now` 里 `is_enabled_to` 未兜底，注册表策略限制会让整块 state 更新失败 → try 包住
- 订阅解析失败时把原有余额数据整个覆盖成空 → 只在有新数据时覆盖
- `int(settings["mixed_port"])` 等裸转 → `_int()` 收敛，脏值回落默认
- 订阅列表写入加锁，避免与后台更新互相覆盖

**configgen.py（2）**：`write()` 原子写（原来 `write_text` 崩一半内核就读半截 YAML）；端口 `_port()` 校验

**purity.py（3）**：缓存原子写；`load_all` 单条脏数据不再连坐整份缓存；`put()` 加锁

**disguise.py（2）**：时区备份从 `data/` 挪到 `%LOCALAPPDATA%\VPNTray\`（原来删 data 就永久丢失原时区），保留旧位置读取迁移；还原成功后删除备份

**api.py（1）**：`group_delay` 遇非 JSON 响应 `.items()` 抛 `AttributeError`，外层只 catch `ApiError` → 显式类型检查

**assets/index.html（5）**：见第二节的专项说明，另有 `api()` 加 20 秒超时 + 检查 HTTP 状态、`poll` 加 in-flight 守卫 + 后台标签页暂停

**webui.py / settings.py / subscription.py（另一批，见 `data/audit/raw-workflow.txt`）**：工具路径穿越、CSRF、非法 JSON 500、`settings.json` 损坏被静默覆盖、订阅缓存非原子、节点名去重等

---

## 三、已知未修（27 条 minor）

**本轮（v1.7）清掉的 30 条 minor**

前端 `assets/index.html`（7 条）
- `renderSubHead` 数值归一：`traffic_left_gb / total_left_gb / days_left` 一律过 `numOf()`，字符串 `"12.5"`、`null`、`"NaN"` 不再让 `toFixed()` 抛错、也不再让 `reduce` 拼出 `"012.5"`
- 订阅列表排序同理走 `_bal()`（原来 `bv - av` 遇到脏值产出 NaN，排序退化成随机）
- 合并开关状态改从 `STATE.sub.mode` 读（原来读 DOM 上的 `.on`，连点两下会用旧值算出同一个目标），并加 `dataset.busy` 防重入
- 聚簇弹层 `showCluster` 拆出 `placeCluster()`，`scroll`/`resize` 时重算坐标，pin 滚出视口就收起（原来是 `position:fixed` 算一次就固定，一滚就飘走）
- `openLocal` 先把 `localMap / localRegions / mapWrap` 三个元素拿到并判空，再改 `LOCAL_ACTIVE`，避免半截脏状态
- 节点分组字典改 `Object.create(null)`：地区名是外部字符串，撞上 `__proto__` / `constructor` 时普通 `{}` 会走原型链拿到 `Function` 再 `.push` 抛错
- （`projectLL` 的 NaN 夹取、`hist` 采样 `Number()` 归一在 v1.5 已修）

Python（23 条）
- `autostart.py` `_command()` 返回 `(命令行, 真实目标)`，`enable()` 校验目标存在才写注册表 —— 否则下次登录弹「找不到 run.pyw」
- `cli.py` `set` / `mode` 捕获 `api.ApiError`，改为打印一行错误并返回码 1，不再抛栈
- `sysproxy.is_enabled_to()` 支持 `http=h:p;https=h:p` 分协议写法，否则健康检查会反复 enable
- `tray_win.py` 系统代理勾选改读 `st["sysproxy"]`（注册表真实状态）而非配置
- `tray_win.py` `_build_node_menu` 跳过 `proxies` 里查不到 type 的名字（「剩余流量：xx GB」这类信息项混进了切换菜单）
- `tray_win.py` 图标加载失败降级为 `log.warning` 并回落系统图标（原来只 catch `win32gui.error`，别的异常直接穿透）
- `disguise._tz_offset_hours` 的 PowerShell 字符串改手工双写单引号（原来用 Python `%r`，`\'` 在 PS 里不是转义，带引号的时区名可注入）
- `disguise.score` 先 `exit_cc.upper()`；`location / asn / company` 非 dict 时按空处理（原 `.get()` 直接 AttributeError，而 score 跑在渲染线程上）
- `disguise.set_timezone_for` 读不到当前时区就拒绝切换（没有原值等于永久丢失用户时区）
- `purity._load` 解析失败时先存一份 `.bad` 副本（原来返回 `{}`，下一次 `put` 把几十个节点的缓存覆盖成 1 条）
- `purity.put` 顺手剔除 3×TTL 之外的过期条目（这份缓存只增不减）
- `purity.score` 对 `info` 及三个子块做类型兜底
- `geolink._http_json` 改 `with requests.Session()` + `with s.get()`，非 dict 响应返回 None
- `geolink.locate` 加 `threading.Lock` 保护缓存、`quote()` 编码查询串、失败结果按 `FAIL_TTL=60s` 负缓存（ip-api 免费额度 45 次/分钟，连点拓扑会打满）
- `paths.ensure_dirs` 每项单独 try + 记日志（原来抛异常等于 windowed 进程双击后毫无反应）；`asset()` 去掉重复的 `_MEIPASS` 比对

**仍未处理（27 条 minor）**：`configgen` 的假节点过滤、`webui.py:395` 日志路径硬编码、
`settings.json` 里 `sub_active` 指向已删订阅时的提示文案、`purity` 的 `FLAG_CC` 对纯中文命名节点失效、
日志跨进程并发写入的乱序等 —— 都是「不触发就没事，触发也只是显示异常」的项。

---

**本轮（v1.6）又清掉的 major（7 条）**

- `subscription.py:175` `_proxies_candidates` → 回环探测超时 0.4s→0.15s，结果缓存 30 秒。实测冷启动 0.331s、热缓存 20 次 <0.001s（原来每次调用最坏 1.6s，刷新订阅时整条 UI 线程被占死）
- `webui.py:209` 新增 `_Server(ThreadingHTTPServer)` 并置 `allow_reuse_address = False` —— Windows 上 `SO_REUSEADDR` 允许抢占别人正在监听的端口，bind 照样成功、两个进程各拿一半请求，端口重试循环永远走不到 `except`
- `purity.py:52` `fetch_exit_info` / `measure_speed` 改用 `with _session(port) as s:`；原来每检测一个节点漏一个连接池，100 个节点两轮就两百个句柄
- `app.py:845` `_wait_idle()` 判据从 `state["busy"]`（谁都能清的 UI 文案）换成真实旗标 `_purity_running / _speed_running`，并在超时时打日志
- `app.py:1029` 自动选优通知：`_verify_and_select` 内部已改写 `group_now`，`best != state["group_now"]` 恒假、通知从未触发 → 调用前先存 `before`
- `run.pyw:57` 第二个实例启动时用 `_open_existing()` 逐个探测 7892..7899，认出「VPN Tray 控制台」就打开浏览器，找不到才弹原提示框
- `webui.py:88` 新增 `_sub_row()`：`app._sub_usable(sub)` 原来在同一个 dict 里调两次，顺手把 `nodes/updated` 走 `_int`/`_num` 兜底

**仍未处理（minor，57 条）**：`index.html` 每帧布局读取、聚簇弹层滚动后脱节、图标加载失败退化成系统通用图标、
`settings.json` 里 `sub_active` 指向已删订阅时的提示文案、日志文件跨进程并发写入的乱序等。
都是「不触发就没事，触发也只是显示异常」的项。

---

## 四、自测

```powershell
# 全部模块语法检查
python -m py_compile vpntray\*.py run.pyw

# 真实浏览器跑一遍 UI（Playwright）：抓 JS 报错 + 拓扑 + 选国 + 日志弹层
python tools\ui_smoke.py
```

本轮验证结果：
- `py_compile` 全绿
- `ui_smoke.py`：**0 个 JS 错误**，100 节点渲染、6 条订阅、拓扑 AI 支线、选国、日志弹层全部正常
- `sysproxy.disable()` 故障注入（`_write` 抛 `OSError`）→ 最终 `ProxyEnable=0`，**PASS**
- `port_free` 对正在监听的端口返回 `False`、关闭后返回 `True`，**PASS**
- `Kernel.start()` 连续 5 次 → watchdog 线程数恒为 1，**PASS**
- `remove_subscription` 5 种下标场景（删首 / 删当前 / 删尾 / 只剩一条 / 越界）→ 全部符合预期，**PASS**
- `_proxies_candidates` 冷启动 0.331s / 热缓存 20 次 0.0000s，**PASS**
- 端口抢占：旧进程占着 7892 时新实例 `bind` 抛 `OSError` 并顺延到下一个端口，**PASS**（原来是静默共享）
- `_Server` 上线后重启两次：`ui=1.6 phase=运行中 subs=6 usable=6`，**PASS**

### v1.7（minor 收尾）自测

- `python -m compileall vpntray\`：**全部通过**
- 单元级冒烟（`data/_t1.py`）：
  - `sysproxy.is_enabled_to('127.0.0.1:7891')` → `True`
  - `disguise.score()` 喂 `{'location':'x','asn':None,'company':123}` / `{}` / 空 tz → 三次均不抛错
  - `purity.put('__probe__', ...)` 后缓存 71 条（原 70 + 1），过期条目被剔除、未误删
  - `autostart._command()` → 目标 `run.pyw` 存在
- `ui_smoke.py`：**0 个 JS 错误**，`标题版本 v1.6`、100 节点、6 条订阅（564.8 GB）、拓扑 AI 支线、选日本 11 点、日志弹层全部正常
- 浏览器实测 `/api/state`：`ui=1.6 phase=运行中 current=🇭🇰|香港-直连 mode=merge total_left=564.77 usable=6 nodes=100 purity=70 ai=🇲🇾|马来西亚-IEPL 02`
- 页脚免责声明：`clientWidth == scrollWidth == 1140`、`font-size 12.5px`、`line-height 23.75`、无 `letter-spacing` —— 无溢出、无压缩，渲染正常
