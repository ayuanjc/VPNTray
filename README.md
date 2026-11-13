# VPN Tray

**[⬇ 下载最新版](https://github.com/ayuanjc/VPNTray/releases/latest)** · Windows 10/11 · 解压即用，不用装 Python

常驻托盘的**分流代理**：内嵌 [mihomo](https://github.com/MetaCubeX/mihomo) 内核，国外走机场、国内直连，
出口切换在本地网页控制台里点一下就完事。不需要管理员权限，不装驱动，不写系统目录。

```
解压 → 双击 VPNTray.exe → 托盘出现橙色盾牌
双击图标 / 右键「打开控制台」 → http://127.0.0.1:7892/
第一次用：同意协议 → 把订阅链接粘进「订阅」→ 添加
```

![控制台](assets/screenshot.png)

> **声明**：本软件是个人学习网络协议与分流技术的本地工具，**不提供任何代理服务、节点、线路或订阅**，
> 也不运营任何服务器。界面里出现的节点、流量、套餐信息全部来自使用者自行配置的第三方订阅，与作者无关。
> 请在遵守所在地法律法规及所用服务商条款的前提下使用，后果自负。

> 装了火绒 / 360 / 电脑管家？它们可能拦住本程序写系统代理（日志里 `重新接管系统代理失败: [WinError 5]`）。
> 放行一次即可，见 [9.1](#91-火绒--360--电脑管家-拦住系统代理)。

---

## 1. 它做了什么

| 环节 | 行为 |
|---|---|
| 订阅 | 多条订阅并存、一键切换（按剩余流量排序）；启动自动拉取，默认每 6 小时更新并热重载 |
| 套餐 | 从订阅里解析「剩余流量 / 到期时间」，左上角常驻显示，快用完变色 |
| 分流 | 内网直连 → 场景模块规则 → 订阅规则 → `GEOSITE,cn` / `GEOIP,CN` 直连 → 其余走代理 |
| 内核 | 托管 `bin/mihomo.exe`（v1.19.32），崩溃自动重启（退避，最多 6 次），孤儿内核自动清理 |
| 系统代理 | 接管 Windows 系统代理到 `127.0.0.1:7891`，退出时精确还原你原来的设置 |
| 选节点 | 先测延迟，再用两次真实 HTTPS 请求验证，只选真的能上网的那个 |
| 健康看护 | 每 45 秒探当前节点，连续失败自动换；每 20 秒检查系统代理有没有被别人改走 |
| 场景模块 | 「一组域名 + 一个出口」写成 `data/modules/*.json`，面板里可增删改，改完热重载 |
| 断网急救 | 12 项逐项体检 + 13 个就地修复动作 + 可复制的纯文本报告 |
| AI 接口 | `/ai/v1` 双端点，36 个工具，三级权限，让 AI 直接读状态、改配置、跑动作 |

端口：混合代理 `7891`、内核 API `9091`、控制台 `7892`、本机 DNS `127.0.0.1:53`（可选），
全部只监听 `127.0.0.1`。特意避开常见客户端用的 `7890 / 7897`，可以和别的代理软件共存。

---

## 2. 五分钟上手

1. 解压到一个**不含中文和空格**的目录（例如 `D:\VPNTray`），双击 `VPNTray.exe`
2. 第一次启动会弹使用协议，点「我已阅读并同意」
3. 打开控制台 → 左栏「订阅」→ 粘贴订阅链接 → 「添加」
4. 回到「概览」，等右上角亮绿灯、显示延迟数字，就通了
5. 打不开网页？左栏「扩展」→「断网急救」→ 一键体检，哪一条红，那一行右边就有对应的修复按钮

---

## 3. 控制台

深色、窄边、一屏一件事。左侧栏分六页：概览 / 节点 / 场景 / 扩展 / 订阅 / 设置。
数据每 1.5 秒刷新一次，切换节点、测速这类耗时动作走后台队列，界面不卡。

### 3.1 概览

当前节点（旗标 + 名称 + 延迟 + 纯净度）、上下行实时速率、累计流量、活动连接数、
当前模式（规则 / 全局 / 直连）、以及四个开关：系统代理、开机自启、自动优选、节点自愈。
下面的流量曲线和日志（最近 200 行，可折叠）跟着刷新。

### 3.2 节点

每行 `序号 · 旗标 · 节点名 · 纯净度 · 延迟`，当前节点高亮，点任意一行即切换。

- **筛选**：顶部一排地区 chip（全部 / 香港 / 日本 / 美国 / 台湾 / 新加坡…，带节点数），
  点一下只看这个地区，再点「全部」取消
- **搜索**：按节点名、地区、`IEPL`、`家宽` 过滤；一旦输入自动回到「全部」，
  避免「选了日本又搜香港 = 一行都没有」
- **排序**：按延迟 / 按纯净度 / 按名称；筛选与排序记在 `localStorage`，下次打开还是原样
- **测速**：行内延迟数字可单点重测（<1 秒）

### 3.2.1 纯净度怎么算

先拿出口 IP 画像，再折算成 0–100 分。画像优先走 One-IP（`ip.net.coffee`），
它给综合信任分与判定标记；挂了回退 `ipapi.is`，再不行回退 `ip-api.com`，
当前用的哪一家显示在分数后面，例如 `纯净度 62（One-IP）· 一般`。

| 分数 | 等级 | 含义 |
|---|---|---|
| 85–100 | 极洁净 | 住宅 / ISP 原生 IP，ChatGPT、流媒体、银行风控都过 |
| 70–84 | 良好 | 能用，个别严格的站会拦 |
| 50–69 | 一般 | 机房 IP，流媒体多半不行，ChatGPT 常被拦 |
| < 50 | 较差 | 被标记的脏 IP，建议别用 |

回退算法的起评分 100：Tor 出口 −45、滥用标记 −40、机房 IP −30、代理标记 −28、
VPN 标记 −22、落地国家与节点标称不符 −18、爬虫库 −15、保留地址 −30。
结果缓存在 `data/purity.json`，24 小时内不重复测。

> 节点名对上就复用缓存，所以换订阅后同名节点秒出分数；名字不同则自动忽略，没有副作用。

### 3.3 场景 / 模块

一个**模块**就是「一组域名 + 一个出口」。内置两个（Steam、GitHub），
面板上「+ 新建」可以随便加，改完立即热重载，不用重启程序。

模块文件：`data/modules/<key>.json`，一个文件一个模块，`key` 就是文件名。

| 字段 | 说明 |
|---|---|
| `key` | 唯一标识，`^[a-z0-9][a-z0-9_-]{1,31}$`，重命名等于新建 |
| `title` | 显示名，也是策略组名，**模块之间不能重名** |
| `kind` | `scene`（建策略组）或 `rule`（只插规则，不建组） |
| `enabled` | 关掉 = 不建组也不插规则，规则直接落到后面的 CN 直连 / 代理 |
| `group` | `{type, auto, auto_title, probe, interval, timeout, target}`，`type` ∈ `select / fallback / url-test / load-balance` |
| `match` | `{domains[], rules[], cidrs[]}`；`domains` 走 `DOMAIN-SUFFIX`，`rules` 是原样规则，`cidrs` 带 `no-resolve` |
| `prefer_regions` | 建组时优先挑这些地区的节点 |
| `pinned` | 钉死的出口节点名；留空 = 交给策略组自己选 |
| `builtin` / `icon` / `order` | 内置标记、图标、排序 |

上限：64 个模块 / 每个 400 条域名 / 200 条规则。规则类型只允许白名单里的 18 种，
目标组由程序拼，不允许你在规则里写 `DIRECT` 之类的旁路——防止一条手写规则把整个分流打穿。

- `group.auto` 且 `kind=scene` 且可用节点 ≥2 时，额外建一个「XX 自动」子组（`url-test`，`tolerance=50`）
- 组名与订阅里的组重名时，本轮跳过该模块并在 `app.log` 记一条 WARNING（重名会让内核起不来）
- 内置模块只在首次运行物化一次（`data/modules/.seeded`）；删掉不会自己复活，
  想恢复就在面板点「恢复内置」，自建模块不受影响
- 模块增删改后：重写 `config.yaml` → 调内核热重载 → 恢复上次选择的出口

规则插入顺序（自上而下逐条匹配，命中即走）：

```
局域网直连 → AI 域名 → 各模块规则（按 order）→ CN 补充规则
→ 订阅规则 → GEOSITE,cn,DIRECT → GEOIP,CN,DIRECT → MATCH,主组
```

### 3.4 订阅

多条订阅并存，`single` 模式只用当前这条，`merge` 模式把所有订阅的节点合成一个池
（同一机场多个账号时用）。列表里显示剩余流量、到期时间、节点数、上次更新时间；
每 6 小时自动更新，也可以点「立即更新」。

### 3.5 扩展

零碎小件都收在这一页：

| 卡片 | 作用 |
|---|---|
| **断网急救** | 12 项体检 + 就地修复 + 报告复制，见 [第 4 章](#4-断网急救) |
| **GitHub 链路** | 五项链路自检（DNS / TCP / TLS / 响应 / 实际拉取），失败可自动把出口切到能连上的节点 |
| **本机解析** | 拉起 `127.0.0.1:53` 的本地 DNS 兜底，专门伺候 Node / c-ares 这类绕过系统代理的程序 |
| **AI 通道** | AI 专用出口组的当前节点、出口 IP、时区对齐、启动器 |
| **AI 接口** | token、权限级别、工具清单，见 [第 5 章](#5-ai-接口) |

### 3.6 设置

运行模式（规则 / 全局 / 直连）、开机自启、系统代理开关、日志级别、检查更新、
以及维护动作：纯净度（当前 / 全部）、带宽测速、重启内核、断开全部连接、扫描代理冲突、退出程序。
所有确认框都是页面内自绘弹窗（Enter 确定 / Esc 取消），不弹 Windows 原生对话框。

---

## 4. 断网急救

「网突然不能用了」在这一页变成可点的一屏。一轮体检 5～7 秒，跑在独立线程里，面板轮询不受影响。

![断网急救](assets/screenshot-tools.png)

### 4.1 12 项体检

| 项 | 看什么 |
|---|---|
| 网卡 / 链路 | 拿到哪个 IP、属于家用 / 校园 / NAT、有没有默认网关 |
| 网关连通 | ping 网关的丢包与延迟（判断是本地链路还是上游） |
| 系统 DNS 配置 | 指向谁、是静态还是 DHCP、有没有兜底 |
| 本机解析 53 | `127.0.0.1:53` 有没有人在解析，是否该开 |
| 域名解析 | 用系统 DNS 和公共 DNS 各问一次同一个域名，区分「本机 DNS 坏了」和「真的断网」 |
| 系统代理 | 指向 `127.0.0.1:7891` 但没人监听？还是被别的软件改了？ |
| 代理内核 | `7891 / 9091` 两个端口在不在听 |
| 直连出口 | 绕过代理打 204 探针：通、不通、还是被门户劫持 |
| 链路 MTU | `ping -f` 倒推 MTU，1428 是 PPPoE 的典型值；分片全丢也能看出来 |
| hosts 文件 | 几条屏蔽、几条重复、文件是不是被设成只读 |
| 代理软件冲突 | 别的 Clash / v2rayN / sing-box 在抢系统代理或 TUN |
| IPv6 | 有没有全局地址（没有不影响 IPv4 上网） |

每条都给**证据**而不是结论：具体 IP、具体毫秒、具体端口、具体文件名。

### 4.2 13 个修复动作

| 动作 | 做什么 | 管理员 |
|---|---|---|
| `flush_dns` | `ipconfig /flushdns` | 否 |
| `fix_proxy` | 清空系统代理并关掉「系统代理」意图 | 否 |
| `start_dns` | 拉起本机解析 `127.0.0.1:53` | 否 |
| `restart_kernel` | 重启 mihomo（连接会断一下） | 否 |
| `fix_dns` | 网卡 DNS 固定成公共 DNS | 是 |
| `fix_dns_keep_local` | 给现有 DNS 补一个公共兜底 | 是 |
| `dns_auto` | 网卡 DNS 改回自动获取 | 是 |
| `renew_ip` | `ipconfig /release` + `/renew` | 否 |
| `restart_adapter` | 禁用再启用默认网卡（断网约 3 秒） | 是 |
| `winsock_reset` | `netsh winsock reset` + `int ip reset`（要重启） | 是 |
| `fix_hosts` | 备份 → 删屏蔽条目 → 补回标准 localhost → 按域名去重 → `flushdns` | 是 |
| `open_portal` | 打开校园网 / 酒店认证页 | 否 |

`fix_hosts` 的具体规则：

- 只删「把域名指到 `0.0.0.0` / `127.0.0.1` / `::1` / `::`」的行，
  `127.0.0.1 localhost` 这种标准行留着
- 你自己写的正常映射（`10.0.0.9 nas.lan`）**一律不动**
- 同一域名出现多次只留第一条；注释和空行原样保留
- 先备份成 `hosts.vpntray-<时间戳>.bak`，同时留一份固定的 `hosts.vpntray.bak`
- 文件被破解补丁设成只读也能处理，写完自动 `flushdns`

**一键急救**只跑安全的四项：清缓存 → 清系统代理 → 拉起本机解析 → 重启内核。
要管理员权限的动作（改 DNS、重启网卡、Winsock、hosts）只列出来不硬干——
这个程序不会替你决定断网三秒。

单项修完会**就地复检**那一项，红的当场变绿，不用重跑整轮。

### 4.3 报告

点「复制」得到一份纯文本，坏的在最上面：

```
[BAD ] 系统代理：还挂着 127.0.0.1:7891，但没人监听
       ProxyEnable=1 ProxyServer=127.0.0.1:7891
       建议：清空系统代理（恢复直连）
[OK  ] 网关连通：网关 172.50.86.1 3ms 无丢包
[WARN] hosts 文件：3 条屏蔽（internetdownloadmanager.com, live.altium.com…）
       建议：备份并修复 hosts
```

### 4.4 命令与 AI 接口

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/net/diagnose` | POST | 跑一轮体检（`{"refresh":true}` 强制重跑） |
| `/api/net/repair` | POST | 一键急救（默认安全四项） |
| `/api/net/fix` | POST | 单项修复 `{"action":"fix_hosts"}` |
| `/api/net/report` | POST | 拿纯文本报告 |

AI 侧对应四个工具：`net.diagnose` / `net.report`（read）、`net.fix` / `net.repair`（full）。

---

## 5. AI 接口

程序对外只开两个端点，别的什么都不暴露：

```
GET  /ai/v1/manifest   → 版本、权限级别、工具清单与参数说明
POST /ai/v1/call       → {"tool":"modules.list","args":{}} → {"ok":true,...}
```

鉴权：token 在 `data/settings.json` 的 `ai_api_token`（首次启动生成 32 位），
三种传法任选 —— `X-AI-Token: <token>`、`Authorization: Bearer <token>`、`?token=<token>`。
面板「扩展 → AI 接口」里能看、能复制、能重置。

```bash
curl -s http://127.0.0.1:7892/ai/v1/manifest -H "X-AI-Token: $TOKEN" | jq .level
curl -s -X POST http://127.0.0.1:7892/ai/v1/call -H "X-AI-Token: $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"tool":"net.repair","args":{}}'
```

三级权限（`settings.ai_api_level`）：

| 级别 | 能力 | 典型工具 |
|---|---|---|
| `read` | 只看不动 | `status` `modules.list` `nodes.list` `subs.list` `log.tail` `net.diagnose` |
| `write` | 改配置、切节点 | `modules.save` `modules.delete` `scene.select` `node.select` `mode.set` `sub.*` |
| `full` | 会断网 / 会退出的动作 | `kernel.restart` `proxy.system` `winsock` 类、`test.speed` `update.apply` `app.quit` |

36 个工具，分四组：状态与清单（read）、模块与出口（write）、内核与系统（full）、网络体检（read/full 混合）。
超权限调用返回 `{"ok":false,"error":"「重启内核」需要 full 权限，当前上限是 write","need":"full"}`，
不会半执行。`test.speed` / `test.purity` 是后台排队，立刻返回 `{"ok":true,"queued":true}`。

> 想彻底关掉：面板里关「AI 接口」，或者把 `ai_api_enabled` 设成 `false`，之后 `/ai/v1` 一律 401。

---

## 6. 托盘菜单

左键双击打开控制台；右键菜单从上到下：

- 状态（当前节点 · 延迟）、实时速率、当前订阅的账户信息
- **切换节点**（子菜单，按地区分组，当前节点打勾）
- 模式：规则分流 / 全局代理 / 全部直连
- 测延迟、自动优选、节点自愈、系统代理、开机自启、本机解析
- 打开控制面板、断开全部连接、重启内核、打开日志目录、退出

---

## 7. 目录结构

```
VPNTray/
├── VPNTray.exe            主程序（onedir）
├── _internal/             Python 运行时 + 打包进去的 assets（别删）
├── bin/
│   ├── mihomo.exe         内核
│   └── GeoIP.dat / GeoSite.dat / country.mmdb
├── data/                  运行时数据（删了会自动重建）
│   ├── config.yaml        由订阅 + 模块生成的配置
│   ├── settings.json      你的设置（含 AI token）
│   ├── modules/           场景 / 规则模块，一个文件一个模块
│   ├── subscriptions.json 订阅列表
│   ├── sub_cache.yaml     订阅原始备份
│   ├── purity.json        纯净度缓存
│   ├── sysproxy_backup.json  接管前的系统代理设置
│   ├── feedback/          生成的反馈包
│   ├── ui/                内核自带的 metacubexd 面板
│   └── logs/              app.log / kernel.log
└── 首次使用.txt / README.md
```

源码树里另有 `vpntray/`（Python 包）、`assets/`（控制台单文件前端 + 图标）、
`tools/`（自检与辅助脚本）、`tests/`（单元测试）。

---

## 8. 关键设置（`data/settings.json`）

| 键 | 默认 | 说明 |
|---|---|---|
| `sub_url` / `sub_active` | — / `0` | 老字段，迁移到 `subscriptions.json` 后只读 |
| `sub_mode` | `single` | `single` 只用当前订阅；`merge` 合并所有订阅的节点 |
| `sub_update_hours` | `6` | 订阅自动更新间隔，`0` = 关闭 |
| `mixed_port` / `controller_port` | `7891` / `9091` | 混合代理端口 / 内核 API 端口 |
| `system_proxy` | `true` | 启动后接管 Windows 系统代理 |
| `mode` | `rule` | `rule` / `global` / `direct` |
| `delay_interval_min` | `20` | 延迟刷新间隔，`0` = 关闭 |
| `auto_best` | `false` | 定时切到最快节点（会覆盖手动选择） |
| `heal` | `true` | 当前节点失效自动换 |
| `dns_guard` | `true` | 本机 DNS 兜底 `127.0.0.1:53` |
| `scene_auto` | `true` | 给带 `group.auto` 的场景建「XX 自动」子组 |
| `ai_api_enabled` | `true` | AI 接口总开关 |
| `ai_api_level` | `full` | AI 权限上限 `read` / `write` / `full` |
| `ai_api_token` | 自动生成 | AI 接口 token（32 位，重置会换新） |
| `auto_update` / `update_repo` | `true` / `ayuanjc/VPNTray` | 更新检查 |

> `steam_*` / `github_*` 等场景键在 2.5.9 起废弃，只在首次把老配置迁移成
> `data/modules/*.json` 时读一次；之后一切以模块文件为准。

---

## 9. 常见问题

### 9.1 火绒 / 360 / 电脑管家 拦住系统代理

表现：控制台「系统代理」是关的、日志刷 `[WinError 5]`、浏览器仍然直连。

1. 安全软件 → 信任区 / 白名单 → 添加 `VPNTray.exe`
2. 还在拦就进「系统防护 → 自定义规则」，给 `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings` 放行写入
3. 确认放行：控制台点一次「系统代理」开关，看日志是否出现 `系统代理已开启 -> 127.0.0.1:7891`
4. 已经断网了：进「扩展 → 断网急救」点「一键体检」，系统代理那一条会告诉你是「没人监听」还是「被改走」

### 9.2 网页全打不开，但托盘显示已连接

按顺序看「断网急救」三项：网关是否通 → 域名解析是否通 → 直连 204 是否通。
网关不通 = 本地链路问题；网关通但域名不通 = DNS 被改坏；都通但代理不通 = 内核或系统代理的问题。

### 9.3 日志里「订阅的 fake-ip-range 不是保留地址段」

这不是报错，是护栏。有些机场订阅写着 `fake-ip-range: 28.0.0.1/8`，
而 `28.0.0.0/8` 是真实公网段，拿它当虚拟 IP 池会让半个互联网解析异常。
程序只接受 `198.18.0.0/15` 和 `100.64.0.0/10`（前缀不宽于 /16），不合规就回退 `198.18.0.1/16` 并记 WARNING。
同类警告「订阅开启了 dns.ipv6，但本机没有 IPv6 出口，已关闭」同理，都不用管。

### 9.4 双击图标没反应 / 托盘里没有

看 `data/logs/app.log` 最后几行。常见两种：单文件版被杀软卡在解包，换 onedir 版；
或安全软件把进程挂起，加白名单后重启。

### 9.5 AI 面板全红 `cannot resolve` / `local name resolution failed`

Node / c-ares 不吃系统代理也不吃 hosts，只认系统 DNS。到「扩展 → 本机解析」打开兜底，
它就是 `127.0.0.1:53` 的本地解析服务。

### 9.6 Windows 拦了一句「Windows 已保护你的电脑」（SmartScreen）

这行提示不是杀毒软件的结论，是「这个 exe 没有数字签名、下载量还不够多」，Windows 拿不准，先拦一下。共三种处理方式，从省事到彻底：

1. **解压之前先解锁 zip**（推荐）：右键下载到的 zip → 属性 → 最下面勾「解除锁定」→ 确定 → 再解压。标记不会传给解压出来的文件，双击 exe 就不再弹。
2. **已经解压了**：双击包里的 `解除锁定.cmd`，它对本目录跑一遍 `Unblock-File -Recurse`，等价于对每个文件做第 1 步。
3. **点「更多信息」→「仍要运行」**：只对当前这个文件生效，下次换版本还会弹。

想确认文件没被人替换过，对着包里的 `SHA256SUMS.txt` 核一遍：

```powershell
Get-FileHash .\VPNTray.exe -Algorithm SHA256
Get-FileHash .\bin\mihomo.exe -Algorithm SHA256
```

彻底消掉这个提示只有一条路：买代码签名证书（OV 约千元/年，EV 一次性几千且要硬件令牌）并在 release.ps1 里加 `signtool sign`。没有证书时，程序自己能做的上限是「启动后把目录里所有文件的 Internet 标记清掉」（`ui_native.unblock_tree`），但那是拦完以后的事了。

### 9.7 同时开着别的代理软件

它们会抢同一套东西：系统代理注册表、PAC、TUN 虚拟网卡。控制台「扫描代理冲突」会列出
系统代理状态、其他代理进程、Up 状态的 TUN 网卡，然后三个按钮分别处理（每一步都弹确认）：

| 按钮 | 做什么 | 影响 |
|---|---|---|
| 关闭系统代理 | `ProxyEnable=0` 并删掉 `ProxyServer` / `AutoConfigURL` | 立刻走直连 |
| 结束冲突进程 | `taskkill` 列出来的进程 | 会关掉你别的代理软件 |
| 禁用 TUN 网卡 | `Disable-NetAdapter` | 需要管理员 |

自己的进程和 mihomo 永远排除在列表外；清系统代理时会把「系统代理」开关一起记成关闭，
否则健康看护 20 秒后又装回去。

---

## 10. 从源码运行 / 打包 / 测试

```powershell
python -m pip install pystray pillow pyyaml requests pywin32
python run.pyw                      # 直接从源码跑

pwsh -File build.ps1                # 单文件 exe（自己用；装了 GamePP 之类会崩的机器别用）
pwsh -File release.ps1              # onedir 发布包（给别人用，启动快、误报少）
python -m pytest tests -q           # 单元测试
```

- 版本号只有一个来源：`vpntray/webui.py` 的 `UI_VER`（`__init__.py` 与前端同步）
- `release.ps1` **必须用 PowerShell 7**（`pwsh`）跑：5.1 会把无 BOM 的 UTF-8 脚本按 ANSI 解析
- 产出 `release/VPNTray-<版本>-win64.zip`，不含任何订阅与你的设置，
  但带一份 `data/purity.json`（同名节点直接有分数）

启动参数：`--console` 启动后打开控制台、`--no-web` 不启控制台、`--pystray` 用备用托盘。

---

## 11. 更新日志

### 3.0.1 — 检查更新说「未配置 update_repo」已修

- 更新仓库填错/留空不再是一句干巴巴的报错：`updater.normalize_repo()` 会把 `https://github.com/owner/name`、`owner/name/`、`owner/name.git`、`owner/name/releases/...`、前后空格统一收敛成 `owner/name`，非法字符或只填一半才报错，并说清该写什么。
- 概览页「检查更新」失败时直接给输入框 + 「保存并重查」：填完立刻写盘并重查，不用重启程序（`POST /api/update/repo`，参数 `{"repo":"owner/name"}`）。
- 设置页说明改成：仓库写成 `owner/name`，**留空 = 关闭检查更新**（以前这句只在代码注释里）。
- 测试从 62 增到 73：`tests/test_update.py` 覆盖 URL / 斜杠 / .git / 多级路径 / 非法字符 / 空值 / 保存后自动重查；`tests/test_single_instance.py` 覆盖互斥体判活与进程枚举。
- **顺手修掉的单实例竞态**：以前「杀掉旧进程 → 立刻双击新 exe」会起两个主实例（新实例看到端口全关，把正在启动的旧实例当成空壳直接接管），两套内核抢 7891，系统代理指谁看运气。现在 `run.pyw` 的接管分支先枚举 `VPNTray.exe` 进程：对方还活着就等它（最多 10 秒），只在自己独占时才启动。实测「并发双击两次」只留 1 个主进程 + 1 个窗口子进程。

## 3.0.0 断网急救 + 场景模块化收口

- **断网急救**：12 项体检、13 个修复动作、就地复检、报告可复制；四个 HTTP 端点 + 四个 AI 工具
- **hosts 修复**：备份 → 只删屏蔽行 → 补回标准 localhost → 按域名去重 → flushdns；
  `127.0.0.1 localhost` 不再被误判成屏蔽，自定义映射不动
- **动效**：切页左右滑动、卡片依次浮起、按钮按下回弹、开关弹跳、弹层缩放进入、
  长任务按钮转圈；全部只动 `transform/opacity`，并尊重 `prefers-reduced-motion`
- 修复「复制」在非 https 页面上静默失败（`navigator.clipboard` 不可用时退回 `execCommand`）
- 单元测试 62 个全绿

### 2.5.9 场景变成模块，AI 能直接操作它

- 场景从代码常量搬成 `data/modules/*.json`，面板可增删改，热重载
- `kind=rule` 支持「只插规则不建组」；组名冲突自动跳过并记日志
- `/ai/v1` 双端点、三级权限、token 鉴权，36 个工具

### 2.5.8 fake-ip 护栏

机场把 `fake-ip-range` 设成 `28.0.0.1/8` 时强制回退 `198.18.0.1/16`；
无 IPv6 出口时强制 `dns.ipv6=false`。

### 2.5.7 本机 DNS 兜底

新增 `127.0.0.1:53` 本地解析，救 Node / c-ares 的 `cannot resolve`。

### 2.5.6 修「点了没反应」

排除 setuptools / pkg_resources，避免 WMI 卡死时进程永久阻塞。

### 2.5.4 面板重写

侧栏分页、一屏一件事、自绘确认框，不再弹原生对话框。

### 2.5.0 GitHub 自动组 + 自检

五项链路自检、失败自动切出口、启动时对齐 git 的 http(s).proxy。

更早的版本（2.4 之前的拓扑图、地图、界面演化）见 git 历史。

---

## 12. 反馈与免责

「扩展」页里的反馈入口会收集版本、系统、节点数、订阅条数、当前节点和最近 200 行日志，
生成一份 markdown 存到 `data/feedback/` 并给出一条预填好的 GitHub issue 链接。

**日志在生成时已经脱敏**：订阅链接只留域名，节点 password / uuid 打码，
IPv4 留前两段、IPv6 留第一段，用户目录替换成 `%USERPROFILE%`。发之前自己先看一遍。

- 仓库：<https://github.com/ayuanjc/VPNTray>
- 问题：<https://github.com/ayuanjc/VPNTray/issues>

首次启动会弹使用协议（这是什么 / 你要负责的事 / 数据在哪里 / 免责 / 不同意怎么办），
必须点同意才能继续，同意状态按版本记录，升级后重新提示一次。

本软件不提供任何代理服务与订阅内容；使用者自行承担配置与使用带来的一切后果。
