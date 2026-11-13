# 技术设计 · 诊断内核与 API 医生（1.1 期）

> 配套：产品规格-PRD-1.x.md　基线代码：VPNTray 1.0.1　日期 2026-11-14
> 本文只回答一件事：这些功能用哪些文件、哪些接口、哪些数据结构实现，怎么保证不写成坨。

---

## 1. 现状（先承认体量，再谈加功能）

实测（wc -l vpntray/*.py，2026-11-14）：

| 文件 | 行数 | 说明 |
|---|---|---|
| vpntray/app.py | 3124 | 全部业务逻辑的收口，**已经超载** |
| vpntray/netdoctor.py | 1469 | 13 项体检 + 13 个修复动作 |
| vpntray/webui.py | 1103 | 路由 + 状态打包 + 静态服务 |
| vpntray/modules.py | 801 | 场景模块（12 个内置） |
| vpntray/campus.py | 654 | 校园网守护 |
| vpntray/aiops.py | 622 | 40 个 AI 工具 |
| vpntray/okxguard.py | 508 | 交易通道守护 |
| 合计（含其余） | 17774 | 测试 257 例 |

**结论**：app.py 已是历史包袱。本次功能一行都不许往 app.py 里堆业务，只能在那里留接线（不超过 20 行）。

---

## 2. 参考项目与取舍

### 2.1 内核（mihomo）：直接读，不自己抓包

实测运行中的 GET http://127.0.0.1:9091/connections（v1.19.32）返回：

| 层级 | 字段 |
|---|---|
| 顶层 | downloadTotal / uploadTotal / connections / memory |
| 连接 | id / metadata / upload / download / start / chains / providerChains / rule / rulePayload |
| metadata | network type sourceIP sourcePort destinationIP destinationPort inboundIP inboundPort inboundName host dnsMode process processPath sniffHost remoteDestination sourceGeoIP destinationGeoIP uid |

现实样本（线上抓的）：network=tcp、type=HTTPS、inboundPort=7891、host=steamcommunity.com、rule=DomainSuffix、chains=[香港-优化, Steam 加速]。

**取用**：事件采集只需要 host / type / inboundPort / processPath / chains / rule / start。不需要 MITM、不需要抓包、不需要装驱动。processPath 为空说明连接不是当前用户态程序起的，不能当成「用户正在用某个 AI 工具」的强证据。

### 2.2 同类 GUI 项目

| 项目 | 技术栈 | 值得学 | 不学 |
|---|---|---|---|
| Clash Verge Rev | Tauri(Rust) + React | 配置分「原始订阅 + 覆写 + 脚本」三层，可复现可 diff；内核生命周期集中在一个 service | 前端状态全塞一个 store，逻辑与视图不分 |
| mihomo-party | Electron + React | 内核启停/日志/配置边界清楚，UI 只读 IPC | Electron 体积（我们 onedir 已 66MB） |
| FlClash | Flutter | 平台差异收敛在一个 platform 层，业务代码一份 | 跨端抽象成本，我们只做 Windows |
| 我们 | Python + threading + 自研 HTTP + 单文件 index.html | —— | —— |

可迁移的只有一件事：单向依赖 + 单一职责。技术栈不同，但谁调谁的规矩可以照抄。

### 2.3 其他思想来源

| 来源 | 借什么 |
|---|---|
| OpenTelemetry | 事件 = 时间戳 + 名称 + 结构化属性；属性名走固定词表，不随意加字段 |
| Sentry fingerprint | 同一类问题合并成一条，不按每条报错刷屏 |
| Wireshark | 采集与分析解耦：先落盘原始事实，分析随时可重跑 |
| NetLimiter / GlassWire | 「进程 - 连接 - 域名」的视角，比「域名 - 规则」更接近用户心智 |

---

## 3. 设计原则（违反任一条，评审直接退回）

1. **单一职责**：一个文件一件事。超预算就拆，不靠注释分区续命。
2. **单向依赖**：纯逻辑 ← 引擎 ← 采集 ← 接线。下层不许 import 上层。
3. **纯函数优先**：解析、分类、匹配、排版一律无 IO、无全局状态。
4. **契约先行**：事件、动作、报告、pack、文案五种结构都带 schema 版本，读时升版。
5. **失败显性**：抓不到就报抓不到，不许返回空列表假装正常（这是本仓最贵的 bug 类型）。
6. **预算制**：每个新文件有行数上限，超了就拆；app.py 净增不超过 20 行。
7. **文档即测试**：本文每条 schema，测试里必须有对应的校验。

---

## 4. 目录与文件（含行数预算）

```
vpntray/diag/               新子包，1.1 期全部代码在这里
  __init__.py        ≤60    公开 API：start(app) / stop() / status(app) / diagnose(app, ...)
  model.py          ≤220    Event / Action / Report / Fingerprint 的 dataclass 与 to_json/from_json
  classify.py       ≤260    纯函数：provider 识别、错误体解析、分类 A/B/C
  paste.py          ≤180    纯函数：从任意文本里抽 status / provider / body / request_id
  packs.py          ≤260    pack 加载、校验、缓存、匹配（含正则安全检查）
  actions.py        ≤320    动作注册表、before/after 快照、撤销、互斥
  store.py          ≤220    JSONL 追加、轮转、尾部查询（events / actions）
  collect.py        ≤260    代理层采集线程（轮询 /connections，去重、限流）
  engine.py         ≤300    diagnose / fix / verify 编排（唯一知道所有部件的地方）
  api.py            ≤220    HTTP 处理函数（参数解析 + 调 engine + 组织返回）
vpntray/copy.py     ≤160    文案层（load / t(key, level) / 缺失告警）
data/packs/api-doctor.json 指纹库（数据，不是代码）
data/copy/zh-CN.json       文案（数据）
assets/js/diag.js   ≤520    诊断页前端（从 index.html 分出来，见 §11）
```

允许的 import：

```
model / classify / paste / packs   ->  stdlib + vpntray.logger + vpntray.paths
actions                            ->  上一行 + vpntray.settings（只通过传入对象读）
store                              ->  上一行
collect                            ->  以上 + vpntray.kernel（只读 api）
engine                             ->  以上全部 + app 的白名单方法（鸭子类型）
api                                ->  engine + copy
```

硬禁止：vpntray/diag/* 不许 import vpntray.app / webui / tray_win / ui_native，也不许反向依赖。

---

## 5. 分层与数据流

```
 采集层 collect.py --+
 粘贴 paste.py ------+--> Event --> packs.match() --> Report --> UI / AI
 网关（1.3 期）-----+        |                        |
                              v                        v
                        store.events           actions.fix() --> verify() --> store.actions
```

一次诊断 = Event 到 Report（纯计算，不含任何修复）。一次修复 = Report 里的 action 到 fix 到 verify 到 Action。
两者分开，是为了「只想看结论」和「要动手」能分别测试、分别授权。

---

## 6. 数据结构（契约）

### 6.1 Event v1

```json
{
  "schema": 1,
  "id": "evt_20261114T004912_a3f1",
  "ts": 1763051352.41,
  "source": "proxy",            // proxy | paste | gateway | probe
  "provider": "deepseek",       // deepseek | qwen | openai | anthropic | unknown
  "host": "api.deepseek.com",
  "category": null,             // A | B | C，由 classify 填
  "status": null,               // HTTP 状态码；看不到就是 null，不是 0
  "ms": 2410,
  "exit_ip": "50.7.250.50",
  "node": "香港-优化3",
  "process": "",                // processPath 后两段，能识别才填
  "fingerprint": null,
  "request_id": "",
  "snippet": ""                 // 脱敏后原始片段，不超过 200 字
}
```

### 6.2 Action v1

```json
{
  "schema": 1,
  "id": "act_x", "event_id": "evt_x",
  "action": "switch_node",
  "by": "user",                 // user | ai
  "before": {"node": "香港-优化3"},
  "after":  {"node": "日本-优化1"},
  "result": "ok",               // ok | failed | need_admin | undone
  "verify": {"status": 200, "ms": 930},
  "ts": 1763051400, "undone": false
}
```

### 6.3 Report v1（同时给 UI 和 AI）

```json
{
  "ok": true, "schema": 1, "event": "evt_x",
  "known": true,
  "category": "A",
  "fingerprint": "api.auth.invalid_key",
  "title_key": "api.auth.invalid_key.title",
  "title": "这根 key 已经失效了",
  "why": "上游返回 401 且响应体含 invalid_api_key",
  "evidence": ["HTTP 401", "响应体含 invalid_api_key", "换节点仍 401"],
  "actions": [{"id": "open_key_page", "title": "打开密钥页", "effect": "只打开网页", "undo": false, "need_admin": false}],
  "pack": "api-doctor", "pack_ver": 3
}
```

title 与 why 由 copy.t() 解析；title_key 一并返回，前端切换白话/术语时不用重新求值。

### 6.4 Pack v1

```json
{
  "schema": 1, "key": "api-doctor", "ver": 3, "title": "API 报错诊断",
  "fingerprints": [
    {
      "id": "api.auth.invalid_key",
      "category": "A",
      "priority": 100,
      "match": {
        "provider": ["deepseek", "openai", "qwen", "anthropic"],
        "status": [401],
        "body_re": "invalid.?api.?key|Incorrect API key|authentication_error"
      },
      "title_key": "api.auth.invalid_key.title",
      "why_key": "api.auth.invalid_key.why",
      "actions": ["open_key_page"]
    }
  ]
}
```

匹配规则（packs.match(evt)，按 priority 降序，先命中先返回）：

1. provider 命中其一（空数组表示不限）；
2. status 命中其一（null 用 0 表示「看不到状态码」）；
3. body_re 在 snippet 上做一次 re.search；
4. 三个条件都满足才算命中；全不命中返回 known=false 的兜底 Report（照样给结论：这条库里没有，已存档，可一键复制报告）。

正则安全：长度不超过 200；禁止前瞻与后顾、禁止嵌套量词；加载时编译一次并缓存；不合规就丢弃该条并 log.warning（带 pack 与 id）。不引入第三方正则引擎。

### 6.5 Copy v1

```json
{ "schema": 1, "lang": "zh-CN",
  "api.auth.invalid_key.title": {"plain": "这根 key 失效了", "term": "API key 无效（401）"},
  "api.auth.invalid_key.why":   {"plain": "...", "term": "..."} }
```

---

## 7. 采集层

### 7.1 代理层（1.1 期就做）

```python
class ProxyCollector:
    def __init__(self, app, interval=2.0): ...
    def start(self): ...          # daemon 线程，Event 控制退出
    def stop(self, timeout=3.0): ...
    def tick(self) -> list: ...   # 取一次快照，返回新增事件（可单测）
```

做法：每 2 秒 GET /connections；对每条连接用 (host, destinationPort) 做去重键，只在「首次出现」和「消失」时各记一条，中间不重复。

- 只处理 type 属于 HTTPS/HTTP/TLS 或 destinationPort 为 443/80 的连接；
- 只有 4 家已知 provider 域名升级成 Event（api.deepseek.com、dashscope.aliyuncs.com、api.openai.com、api.anthropic.com），其他域名只计数不落盘（避免事件表变成浏览记录，这是隐私红线）；
- 每分钟上限 200 条，超了丢弃并累加 dropped，status() 里能看到；
- 抓不到 /connections（内核没起、secret 变了）时不抛异常，status.last_error 记原因，下一轮重试。

### 7.2 粘贴（1.1 期就做，纯函数）

```python
def parse(text: str) -> dict:   # {provider, status, body, request_id, confidence}
```

要覆盖四种真实贴法：①只贴 Error code: 401 - {...}；②贴了整段 traceback（取最后一个 Error code，或用 body_re 兜）；③openai 库的 BadRequestError: Error code: 400 - {...}；④DashScope 的 {"code":"Arrearage"}（没有状态码）。

### 7.3 本地网关（1.3 期，本次只留接口位）

127.0.0.1:8787 反向代理四家上游，落 status/body/request_id。这次不实现，但 Event.source 枚举预留 gateway，将来不用改 schema。

---

## 8. 动作层

```python
@dataclass(frozen=True)
class ActionSpec:
    id: str; title_key: str; need_admin: bool; undoable: bool
    run: Callable      # (app, ctx) -> {ok, before, after, msg}
    undo: Callable     # (app, ctx) -> {ok, msg}，不可撤销时 None
```

注册表 ACTIONS 是 dict。1.1 期先落四个真能用的：switch_node、flush_dns、restart_kernel、fix_proxy；其余六个（clock_sync / set_dns / set_mtu / start_dns_guard / clean_hosts / disable_ipv6）在 1.2 期随 DNS 域一起接。清单在 PRD 里，本文只保证注册表装得下。

规则：

1. 同一时刻只允许一个动作在跑（threading.Lock 加 busy 状态），第二个请求直接返回「上一个动作还没结束」；
2. 动手前必须能取到 before 快照，取不到就不执行（不知道原值就不改）；
3. need_admin 且当前不是管理员，返回 need_admin，不尝试提权；
4. 执行完立刻 verify：网络类动作 3 秒内复测一次，结果写进 Action.verify；
5. 撤销窗口 10 分钟，超时返回 expired（不留半改状态，也不假装能撤）；
6. by=ai 时写日志并在托盘通知一次（用户得知道是谁动的）。

---

## 9. 并发与生命周期

| 线程 | 数量 | 退出方式 | 说明 |
|---|---|---|---|
| diag-collect | 1 | Event.set() 加 join(3) | 采集，2 秒一轮，不与 UI 抢锁 |
| diag-writer | 1 | 队列哨兵加 join(3) | JSONL 串行落盘，UI 线程不直接写文件 |
| HTTP 请求线程 | n | 现有 ThreadingHTTPServer | 诊断不超过 3 秒（纯计算），修复走后台 |
| 动作线程 | 最多 1 | 动作完成即退 | 长动作（重启内核）不占请求线程 |

锁只有两把：_write_lock（落盘）、_action_lock（动作互斥）。禁止用 app.state 里的布尔当锁，1.0.0 的 state['busy'] 已经证明会卡死轮询。

启动：app.start() 末尾 diag.start(self)（放在 campus/okxguard 之后，因为依赖内核就绪）。退出：diag.stop() 先，再 okxguard、campus、dnsguard，最后内核。

---

## 10. HTTP 与 AI 接口

| 方法 | 路径 | 入参 | 出参 |
|---|---|---|---|
| GET | /api/diag/state | - | {ok, diag:{ok,events,last_error,collecting,dropped,level}} |
| POST | /api/diag/diagnose | {text} 或 {host,status,body} | Report v1 |
| POST | /api/diag/fix | {action, event} | {ok, action, report?} |
| POST | /api/diag/undo | {id} | {ok, action} |
| POST | /api/diag/events | {limit, since} | {ok, events} |
| POST | /api/diag/level | {level} | {ok, level} |
| POST | /api/diag/report | {format} | {ok, text} |
| GET | /api/packs | - | {ok, packs} |
| POST | /api/pack/save | {pack} | {ok, error?} |
| POST | /api/pack/delete | {key} | {ok} |

全部沿用现有约定：JSON、Content-Type 必须是 application/json、同源校验、严格布尔、异常映射 500 且带 kind。
路由表由 vpntray/diag/api.py 导出 routes(app)，webui._table() 只 update() 一次，webui 不再长胖。

AI 工具（aiops 加 6 个，40 到 46）：

| 工具 | 级别 | 作用 |
|---|---|---|
| diag.status | read | 采集是否在跑、最近错误、事件数 |
| diag.events | read | 最近 N 条事件 |
| diag.diagnose | read | 传文本或字段，拿 Report |
| diag.fix | full | 执行动作（要求显式 confirm） |
| diag.undo | full | 撤销 |
| diag.packs | read | 有哪些指纹 |

---

## 11. 前端（这次必须拆文件，否则 index.html 会到 3000 行）

现状：assets/index.html 91KB 单文件、内联两段 script；do_GET 只认 /、/favicon.ico、/world_map.js、/local_map.js（webui.py 1018-1056 行实测）。

改动 1（后端，10 行内）：静态白名单加一条按名字取 js：

```python
if path.startswith("/js/") and path.endswith(".js"):
    name = path[4:-3]
    if re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        f = asset("js", name + ".js")      # paths.asset 支持多段
        if f.exists():
            return self._send(200, f.read_bytes(),
                              "application/javascript; charset=utf-8")
```

要点：只允许小写字母数字下划线横线的名字，禁止点与斜杠，天然挡掉路径穿越；CSP 需要把 script-src 从 self 之外补上即可（同源，本来就在 self 里）。

改动 2（前端）：诊断页所有逻辑进 assets/js/diag.js，index.html 只做三件事：加 nav 项、加 section 骨架、加载脚本并在 renderView 的 diag 分支调 renderDiag(S)。

预算：diag.js 不超过 520 行；index.html 只增不超过 40 行。这也是以后所有新页面的规矩。

---

## 12. 文案层落地顺序

1. vpntray/copy.py：load() 懒加载 data/copy/zh-CN.json（mtime 变化重载）、t(key, level) 找不到键时返回 key 本身并 log.warning 一次（同一 key 不刷屏）；
2. 只覆盖新页面（诊断页、API 医生、动作标题）；
3. 加测试 test_copy_keys_complete：遍历 data/packs/*.json 里所有 *_key，断言文案里都有；
4. 旧页面（13 项体检、校园网、OCR）在 1.2 期按域迁移，不做一次性大改。

---

## 13. 测试策略

| 层 | 方式 | 目标 |
|---|---|---|
| 纯函数 classify/paste/packs/copy | 单测 + 语料驱动 | 行覆盖 80%；每条指纹一正一反 |
| store | 临时目录真实读写 | 轮转、坏行跳过不整表崩、并发追加不丢 |
| actions | stub app | before 取不到不执行、need_admin、撤销过期、互斥 |
| collect | stub api | 去重、限流、内核挂了不抛、状态可见 |
| engine | stub + 真 pack | Report 形状固定（golden 快照） |
| HTTP | 现有 tests/smoke_http.py 模式 | 新增 10 条路由 + 6 个 AI 工具 |
| 真机 | build/smoke_live.py 扩展 | state.diag 存在、粘贴 401 出 Report、fix 后 verify 有值 |

错误语料 tests/data/api_errors/（一条文件一条真实报错，全部脱敏）：

| 文件 | 内容 |
|---|---|
| deepseek_401.txt | Error code: 401 - error.message=Authentication Fails, type=authentication_error |
| deepseek_402.txt | Insufficient Balance |
| openai_429.txt | Error code: 429 - rate_limit_exceeded |
| openai_400_alt.txt | messages: roles must alternate between user and assistant |
| openai_400_ctx.txt | maximum context length is 128000 tokens |
| qwen_arrearage.json | code=Arrearage, message=Access denied, 但 HTTP 200 |
| qwen_invalid_model.json | code=InvalidParameter, message=Model not exist |
| anthropic_401.json | type=error, error.type=authentication_error |
| anthropic_529.txt | overloaded_error |
| proxy_407.txt | Proxy Authentication Required |
| tls_reset.txt | SSLError: EOF occurred in violation of protocol |
| timeout.txt | ReadTimeout: HTTPSConnectionPool(...): Read timed out. |

---

## 14. 编码规范（每条都对应本仓踩过的坑）

| 禁止 | 为什么（真实事故） | 正确做法 |
|---|---|---|
| 裸 except 后 pass | 掩盖过代理接管失败、备份丢失 | 至少 log.warning 加 exc_info；可降级不可静默 |
| 用 state 里布尔当锁 | 1.0.0 轮询永久停更 | threading.Lock 加 try/finally |
| 模块里 sleep 轮询无退出条件 | 退出时线程挂住 | 一律 Event.wait(interval) |
| 函数里裸写中文给用户看 | 无法切换白话与术语 | 走 copy.t(key) |
| 一个函数超过 60 行 | 历次最难查的 bug 全在长函数里 | 拆成取数据、算、拼三段 |
| 直接以写模式覆盖配置文件 | hosts 编码污染事故 | 临时文件加 fsync 加 os.replace |
| 把订阅或内核返回的字符串拼进 HTML | 存储型 XSS 事故 | 一律 esc() |
| 新增 import 造成环 | import 循环导致启动随机崩 | 依赖图见第 4 节，只许向下 |
| 返回 None 表示成功 | TUN 永远「重启失败」事故 | 显式 bool 或 {ok, err} |
| 复制上一段代码改改复用 | 13 项体检与 13 个动作已有重复 | 抽公共函数，宁可多一层 |

---

## 15. 实施顺序（每步可单独验收）

| 步 | 内容 | 完成标准 | 预估 |
|---|---|---|---|
| S1 | model + copy + packs + api-doctor.json + zh-CN.json | 语料 12 条全部命中断言；未知文本返回 known=false；文案键齐全 | 1 天 |
| S2 | classify + paste + engine.diagnose | 粘贴 deepseek_401 原文得到正确 Report；AI 的 diag.diagnose 可用 | 0.5 天 |
| S3 | store + collect | 真机跑 10 分钟：Only 4 家域名落盘、限流生效、内核挂掉不崩 | 1 天 |
| S4 | actions + engine.fix/undo + api 路由 | 429 场景切节点、verify 200、撤销回原节点；非管理员返回 need_admin | 1 天 |
| S5 | diag.js + 静态白名单 + 三档显示 | 页面能看时间线、粘贴诊断、点动作、看历史；简洁档只剩结论加按钮 | 1 天 |
| S6 | 文档 + 打包 + 部署 + 冒烟 + 发布 1.1.0 | smoke_live 全绿，真机演示 8 条验收 | 0.5 天 |

合计约 5 个工作日。任何一步超预算（文件行数超 20%）就停下来拆，不许先跑起来再说。

---

## 16. 风险与对策

| 风险 | 对策 |
|---|---|
| 采集把事件表变成浏览记录（隐私） | 只落 4 家 provider 域名；其他只计数；事件保留 7 天自动清理 |
| 指纹写歪导致误判 | 每条必须一正一反测试；未知一律 known=false，不猜 |
| 动作把网络改坏 | before 快照加 10 分钟撤销；只碰网络；不做 MITM |
| 又长出第二个 3000 行文件 | 行数预算写进本文；构建脚本加 wc 检查，超预算直接失败 |
| 前端继续膨胀 | 新页面必须独立 js；index.html 每次改动不超过 40 行 |

---

## 17. 与 PRD 的对应

| PRD 章节 | 本文对应 |
|---|---|
| 三、文案层与显示级别 | 6.5、12 |
| 四、诊断内核 | 4 到 9 |
| 五、API 医生 | 7、6.4、13 语料 |
| 六、数据与隐私 | 7.1（只落 4 家域名）、16 |
| 八、八条验收 | 15 的 S1 到 S6 完成标准 |