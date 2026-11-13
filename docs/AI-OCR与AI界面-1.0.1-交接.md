# VPNTray 1.0.1 交接：AI 独立界面 + OCR 桥

> 给下一个窗口的执行说明。前半是**已发生的事实**（别重复验证），后半是**待做**（精确到行号），最后是**坑**。
> 工作目录：`E:\Desktop\AI\intert\vpntray`（源码）｜运行目录：`E:\VPNTray-2.5.7`（onedir，`_internal` + `bin` + `data`）

---

## 0. 一句话现状

1.0.0 已经在用户机器上跑着，并且**已经含 OCR 桥**（托盘进程内守护 + 场景规则）。1.0.1 要做的事只有一件：**把 AI 相关功能（AI 通道 / AI 接口 / OCR 桥）从「扩展」页抽成独立的一级界面**，顺带把 OCR 路由场景变成出厂内置。

---

## 1. 环境事实（不要再查一遍）

| 项 | 值 |
|---|---|
| 运行目录 | `E:\VPNTray-2.5.7\`（onedir；`VPNTray.exe` + `_internal\` + `bin\` + `data\`） |
| 当前版本 | `1.0.0`（`/api/state` 的 `ui` / `ver`） |
| 当前 exe | 8243266 字节，Nov 14 00:17；进程 PID 33584 |
| 面板 | `http://127.0.0.1:7892`（只绑 127.0.0.1，不校验 token，只校验 Host 头） |
| 内核 | mixed `7891` / controller `9091`，`kernel_ok: true` |
| OCR 桥 | `http://127.0.0.1:8791`，模型 `qwen3.5-ocr`，上游 `https://dashscope.aliyuncs.com/compatible-mode/v1`，key 来自 `ALIYUN_API_KEY` |
| 源码 | git HEAD `cbc644f`（3.3.0）；工作区 `UI_VER` / `__version__` 已被用户改成 `1.0.0`（这是他的决定，别改回去） |
| 未提交改动 | `app.py`(+192 含 campus)、`settings.py`、`webui.py`、`__init__.py`、`aiops.py`、`assets/index.html`；新增 `campus.py`、`ocrbridge.py`、`tests/test_campus.py` |

---

## 2. 已经做完并验证过的（1.0.0 里已生效）

1. **`vpntray/ocrbridge.py`（新增）** — 托盘内的 OCR 桥守护
   - `DEFAULT_PORT = 8791`，`DEFAULT_DIR = Path(r"E:\Desktop\AI\wechat\ocr-router")`，`PROBE_TIMEOUT = 1.0`，`START_WAIT = 8.0`，`INTERVAL_MIN = 10`，`CREATE_NO_WINDOW = 0x08000000`
   - 接口：`_resolve_dir(hint)` / `health(port) -> (bool, str)` / `status() -> dict` / `start(*, dir, port)` / `ensure(dir, port)` / `watch(interval, dir, port, enabled, on_change)` / `stop()`
   - 探活走 `GET /health`（不用 TCP connect，否则端口被别人占着会误判"活着"）
   - 只拉起目录里**确实存在 `server.mjs`** 的地方；`Popen([node, server.mjs], cwd=dir, stdin=DEVNULL, stdout/stderr → logs/stdout.log, creationflags=CREATE_NO_WINDOW)`
   - **`stop()` 只停自检线程，故意不杀 node**（OCR 桥与托盘生命周期解耦）
2. **`app.py` 挂载**：`app.py:316–333` 启动块（`ensure` + `watch`，写 `state.ocr_bridge_ok` / `state.ocr_bridge_reason`，异常只 `log.warning`）；`shutdown()` 里 `ocrbridge.stop()`
3. **`settings.py` 新增 4 键**：`ocr_bridge: True`、`ocr_bridge_dir: ""`、`ocr_bridge_port: 8791`、`ocr_bridge_interval: 30`
4. **场景模块 `data/modules/ocr-router.json`**（`kind: "rule"`、`group.target: "DIRECT"`、5 个域名 `dashscope.aliyuncs.com / aliyuncs.com / aliyun.com / aliyuncs.com.cn / aliyun-inc.com`），已复制到 `E:\VPNTray-2.5.7\data\modules\ocr-router.json`
5. **规则已进内核**：`E:\VPNTray-2.5.7\data\config.yaml:1684–1688` 是 `- DOMAIN-SUFFIX,dashscope.aliyuncs.com,DIRECT` 那 5 行；重建后 `4001 rules`、`kernel_ok: true`
6. **日志证据**（`E:\VPNTray-2.5.7\data\logs\app.log`）：
   ```
   00:17:55 [INFO] app: OCR 桥就绪：127.0.0.1:8791（已在运行）
   00:18:27 [INFO] ocrbridge: OCR 桥已就绪：127.0.0.1:8791（pid 28232，E:\Desktop\AI\wechat\ocr-router）
   ```
7. **触发重建的正确姿势**（已验证可用，无鉴权）：
   ```bash
   curl.exe -s -X POST "http://127.0.0.1:7892/api/module/save" -H "Content-Type: application/json" --data-binary "@body.json"
   ```
   `body.json` = `{"module": <ocr-router.json 内容>}`；`app.module_save()` → `modules_mod.save()` → `_rebuild_from_modules()` → 写 config.yaml 并热加载内核。

---

## 3. 待做：1.0.1 —— AI 独立界面

### 3.1 前端 `assets/index.html`（2343 行，单文件）

现有 AI 相关元素全在「扩展」页里：`620–623`（AI 通道卡片）、`625–640`（AI 接口卡片）。**JS 全部按元素 id 取值，位置无关** —— 所以搬 HTML 不用改任何渲染函数。

1. **导航**：`425–450` 的 `<nav id="nav">`，在 `438–441`（tools 按钮）之后插一项：
   ```html
   <button data-view="ai" title="AI">
     <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.6L18.5 9.5 13.9 11.4 12 16l-1.9-4.6L5.5 9.5l4.6-1.9z"/><path d="M18 16.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9z"/></svg>
     <span class="lbl">AI</span>
   </button>
   ```
2. **标题表** `1018–1025` 加：`ai: ['AI', '通道出口 · 本机接口 · OCR 桥'],`；同时把 `tools` 的描述改成 `'GitHub 链路 · 本机解析 · 断网急救 · 校园网'`
3. **顺序表** `1033`：`var ORDER = ['home', 'nodes', 'scenes', 'ai', 'tools', 'subs', 'settings'];`
4. **新视图**：在 `</section>`（`view-tools` 收尾，`642` 行）之前或之后新建
   ```html
   <!-- AI：通道 / 接口 / OCR 桥 -->
   <section class="view" id="view-ai">
     <div class="stack">
       ...（把 620–623 与 625–640 两个 card 原样搬进来）...
       ...（再加 OCR 桥卡片，见 3.2）...
     </div>
   </section>
   ```
   搬走之后「扩展」页只留：断网急救（566–576）、校园网（577–599）、GitHub（600–608）、本机解析（610–618）。
5. **路由** `1818–1825`：
   ```js
   else if (v === 'ai') { renderAI(); renderModulesApi(); renderOcr(); }
   else if (v === 'tools') { renderNetDoc(); renderCampus(); renderGithub(); renderDns(); }
   ```
6. **事件**：`2043–2061` 的委托链加 `else if (a === 'ocr-restart') ocrRestart();`；`ocrOn` 开关走 `2087` 起的 `change` 委托。

### 3.2 OCR 桥卡片（新增）

```html
<section class="card">
  <h2>OCR 桥<span class="hint" id="ocrHint"></span></h2>
  <div class="gwrap" id="ocrPills"></div>
  <div class="acts" style="margin-top:12px">
    <button class="btn" data-act="ocr-restart">重启桥</button>
    <label class="sw" title="托盘内守护：端口没人听就拉起来"><input type="checkbox" id="ocrOn"><i></i></label>
  </div>
  <div class="note" id="ocrNote">图片先送到 127.0.0.1:8791 换成文字再转发给模型。它和代理无关：阿里云域名在规则里直连。</div>
</section>
```

`renderOcr()` 拿 `/api/ocr/status` 渲染：`ok/模型/端口/目录/uptime/requests/ocrOk/ocrFail/cacheHits`。样式复用 `gwrap` + `.pill`（参考 `renderDns()` 的写法）。

### 3.3 后端 `vpntray/webui.py`

1. 路由表（`694–699` 附近，`"/api/module/export": module_export,` 之后）加：
   ```python
   "/api/ocr/status": ocr_status,
   "/api/ocr/restart": ocr_restart,
   ```
2. 处理函数（与 `module_save` 同层）：
   ```python
   def ocr_status(_body):
       from . import ocrbridge
       st = ocrbridge.status()
       st["enabled"] = bool(app.settings.get("ocr_bridge", True))
       return {"ok": True, "ocr": st}

   def ocr_restart(_body):
       from . import ocrbridge
       ocrbridge.restart()          # 见 3.4
       return {"ok": True, "ocr": ocrbridge.status()}
   ```
3. 顶部 import 已有 `ocrbridge`（`app.py:20` 那行风格），按现有模块导入顺序加即可。

### 3.4 `ocrbridge.restart()`（新增函数）

```python
def kill(port: int = DEFAULT_PORT) -> bool:
    """按端口找 node 并结束。只杀 Listen 在 port 上的 node，不做通配。"""
    # Windows: netstat -ano | findstr :8791 → PID → taskkill /PID n /F
    # 或用 psutil（项目未依赖，别引入）；保持 subprocess + netstat 最省事
```
`restart() = kill(port) → sleep 0.5 → start(port=port)`。注意不要用 `stop()`，它只停线程。

### 3.5 出厂内置 `vpntray/modules.py`（关键，别漏）

现在 `ocr-router` 只是别人机器上没有的一个 json。要「换机器自带」必须进 `builtins()`：

- `builtins()` 在 `405–471`，返回 `[steam, github] + _launcher_builtins()`。在 `471` 之前追加一项 `normalize({...})`：
  ```python
  normalize({
      "key": "ocr-router",
      "title": "OCR 桥 / 阿里云直连",
      "desc": "图片转文字走本地 8791；dashscope 等阿里云 AI 端点直连",
      "kind": "rule",
      "builtin": True,
      "icon": "🔍",
      "order": 60,
      "group": {"type": "select", "target": "DIRECT",
                "probe": "https://dashscope.aliyuncs.com/compatible-mode/v1/models"},
      "match": {"domains": ["dashscope.aliyuncs.com", "aliyuncs.com", "aliyun.com",
                            "aliyuncs.com.cn", "aliyun-inc.com"]},
  })
  ```
- **⚠ 门闩**：`ensure_builtins()`（`659–706`）开头 `if seeded.exists(): return 0` —— 已经播种过的机器**不会被补种**。要让 1.0.1 在用户机器上自动出现这项，二选一：
  - (a) `SEEDED` 文件名带版本（`".seeded" → ".seeded-1.0.1"`），代价是老用户被重播一次（`if not _path(k).exists()` 有保护，不会覆盖已有）；
  - (b) 在 `ensure_builtins` 里对 `builtins()` 逐项做「缺失即补」，与 `seeded` 解耦（更干净，推荐）。
- `validate()` 会校验 `kind=rule` 的 `group.target` 必须是 `{组名} | DIRECT | REJECT | PASS`（`configgen.py:425`）。

### 3.6 版本号

- `vpntray/webui.py:26` → `UI_VER = "1.0.1"`
- `vpntray/__init__.py` → `__version__ = "1.0.1"`
- `release.ps1` 只认 `UI_VER` 一个源（正则 `^UI_VER\s*=\s*"(d+.d+(?:.d+)?)"`）

### 3.7 打包与部署

```powershell
# 必须 pwsh（见坑 4）
pwsh -NoProfile -File release.ps1
# 产物：release\VPNTray-1.0.1\  +  release\VPNTray-1.0.1-win64.zip
```

部署到运行目录（`data\` 不动）：
```powershell
# 1) 退出托盘：POST /api/quit 或托盘右键退出
# 2) 备份：Copy-Item E:\VPNTray-2.5.7\VPNTray.exe E:\VPNTray-2.5.7\VPNTray.exe.bak-1.0.1-<HHMMSS>
# 3) 覆盖 exe + _internal + bin（robocopy release\VPNTray-1.0.1 E:\VPNTray-2.5.7 /E /XD data）
# 4) 启动 E:\VPNTray-2.5.7\VPNTray.exe
```

---

## 4. 坑（血泪，别踩第二遍）

1. **`release.ps1` 必须用 `pwsh` 跑**。用 `powershell.exe`（5.1）读 UTF-8 无 BOM 的中文脚本会按 GBK 解码，第 73 行开始语法崩（`Unexpected token ')'`），打包直接失败。实测 exit 1。
2. **`build.ps1` 是 onefile，本机不能用**：装了「游戏加加 GamePP」时一启动就崩，`faulting module = GPP64.dll`，`0xC0000005`。自用只走 `release.ps1`（onedir）。
3. **`release.ps1` 不带 `data/modules`**，出厂模块只靠 `ensure_builtins()` 播种 —— 所以 3.5 那一步不做，发布包就是没有 OCR 场景。
4. **不要新建 Windows 计划任务 / 启动文件夹**（用户明确否决：全部在软件内实现）。
5. **不要动 `C:\Users\ayuan\.dsh\api-gateway\`**（有扣费事故史，独立网关，与本功能无关）。
6. **`ocrbridge.stop()` 不杀 node**。要真停必须按端口杀（`netstat -ano | findstr :8791` → `taskkill /PID <pid> /F`）。
7. **系统代理不影响 OCR 链路**：`sysproxy.py:34–35` 的 `BYPASS` 含 `127.*` 与 `<local>`，所以面板/DSH 访问 8791 走回环；node 到 dashscope 是 node 自己直连（不读 WinINET）。这条链路在**内核全挂的情况下也活**——这是本功能的立足点。
8. `configgen.py:446` 是生成 `DOMAIN-SUFFIX` 的地方；`configgen.py:427` 的 `_target()` 对未知目标降级主组并 WARNING（规则写错不会崩，但会静默走主组）。

---

## 5. 验证清单（改完照着跑）

```bash
# OCR 桥活着
curl.exe -s http://127.0.0.1:8791/health
# 面板版本
curl.exe -s http://127.0.0.1:7892/api/state | tr ',' '\n' | grep -E '"ui"|kernel_ok'
# 规则真的进了内核
grep -c "dashscope\|aliyuncs" "E:/VPNTray-2.5.7/data/config.yaml"     # → 4~5
# 托盘日志
grep -a "OCR 桥" "E:/VPNTray-2.5.7/data/logs/app.log" | tail -3
# 模块列表里有它
curl.exe -s http://127.0.0.1:7892/api/module/export | head -c 400
```

**回归底线**：`python -c "import vpntray.app, vpntray.webui, vpntray.campus, vpntray.ocrbridge, vpntray.modules, vpntray.configgen"` 必须无异常（已验证 `IMPORT OK 1.0.0`）。

---

## 6. 给另一窗口的一句话 brief

> 在 `E:\Desktop\AI\intert\vpntray` 里做 1.0.1：把 `assets/index.html` 的 AI 通道（620–623）与 AI 接口（625–640）两张卡从 `view-tools` 搬到新的 `view-ai`，nav/TITLES/ORDER/renderView 各加一处 `ai`；新增 OCR 桥卡片 + `renderOcr()` + `/api/ocr/status|restart` 两个后端接口 + `ocrbridge.kill/restart`；把 `ocr-router` 写进 `modules.builtins()` 并让 `ensure_builtins` 支持补种；版本号 `UI_VER`/`__version__` → 1.0.1；用 `pwsh -File release.ps1` 打包，退出托盘后覆盖 `E:\VPNTray-2.5.7` 的 exe + `_internal`（`data` 不动），重启后按第 5 节验证。
