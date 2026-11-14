"""本地 Web 控制面板：HTTP 服务 + JSON API。

只监听 127.0.0.1，不做认证（本机回环），但校验 Host 头以防 DNS rebinding。
长耗时操作（测速/更新订阅/重启内核）异步执行，接口立即返回，前端轮询状态。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import __version__ as _APP_VER
from . import aiops, campus, disguise, dnsguard, doctor, okxguard
from .paths import ROOT, asset
from .purity import FLAG_CC

log = logging.getLogger("webui")

WEB_PORT_DEFAULT = 7892
# 前端界面版本；assets/index.html 里的 UI_VER 必须一致，不一致前端会自动刷新
UI_VER = "1.1.1"

# 单页面板的 CSP：脚本/样式必须放行 inline —— assets/index.html 是单文件内联
# 页面（拆出去就得多发几个请求，而且别人正在改这个文件）；其余一律只允许自身，
# 所以即使有人往页面里塞了外链脚本/图片/XHR，浏览器也会直接拦掉。
_CSP = ("default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'")


def _num(v: Any, default: float = 0.0) -> float:
    """把 settings / 前端传来的脏值收敛成有限浮点数。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / ±Inf
        return default
    return f


def _active(s: dict) -> int:
    """当前订阅下标，脏值一律回落到 0。"""
    return _int(s.get("sub_active"), 0, 0, 999)


def _int(v: Any, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    """安全 int：bool 单独处理（bool 是 int 的子类，int(True)==1 会误选下标）。"""
    if isinstance(v, bool):
        return default
    try:
        i = int(_num(v, default))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        i = max(lo, i)
    if hi is not None:
        i = min(hi, i)
    return i


class BadArg(ValueError):
    """入参类型不合法。HTTP 层把它翻成 400，而不是当成内部错误 500。"""


_TRUE_WORDS = {"true", "1", "on", "yes"}
_FALSE_WORDS = {"false", "0", "off", "no"}


def _flag(v: Any, key: str = "参数") -> bool:
    """严格布尔收敛：只认 true/false/1/0/"1"/"0"/on/off。

    为什么不用 bool(v)：bool("false") 是 True、bool("0") 也是 True，前端一个
    字符串手误就能把开关反向 —— 「关掉 AI 接口」实际变成打开，而且没人看得出来。
    收敛不了就报错（400），让调用方自己修，绝不做「差不多算真」的猜测。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUE_WORDS:
            return True
        if s in _FALSE_WORDS:
            return False
    raise BadArg(f"{key} 只能是 true/false/1/0/on/off，收到 {v!r}")


# ────────────────────────── 状态整形 ──────────────────────────
def _total_left(app) -> float | None:
    """合并模式下所有订阅剩余流量之和（未知的不计入）。"""
    total = 0.0
    seen = False
    for sub in app.subscriptions():
        info = sub.get("info")
        left = info.get("traffic_left_gb") if isinstance(info, dict) else None
        if left is None:
            continue
        total += _num(left)
        seen = True
    return round(total, 2) if seen else None


def _sub_row(app, i: int, sub: dict, s: dict) -> dict[str, Any]:
    """单条订阅的展示数据。

    `_sub_usable` 要算过期/流量/节点数，别在同一个 dict 里调两次。
    """
    info = sub.get("info") or {}
    if not isinstance(info, dict):
        info = {}
    usable, why = app._sub_usable(sub)
    return {
        "i": i,
        "name": sub.get("name") or f"订阅 {i + 1}",
        "url": sub.get("url") or "",
        "nodes": _int(sub.get("nodes"), 0, 0),
        "updated": _num(sub.get("updated"), 0),
        "traffic_left_gb": info.get("traffic_left_gb"),
        "traffic_total_gb": info.get("traffic_total_gb"),
        "traffic_text": info.get("traffic_text") or "",
        "expire_text": info.get("expire_text") or "",
        "expire_ts": info.get("expire_ts"),
        "days_left": info.get("days_left"),
        "reset_text": info.get("reset_text") or "",
        "active": i == _active(s),
        "usable": usable,
        "why": why,
    }


def _region_of(name: str) -> tuple[str, str, str]:
    """从节点名解析 (flag, 中文地区名, ISO 国家码)。"""
    flag = name[:2] if name and 0x1F1E6 <= ord(name[0]) <= 0x1F1FF else ""
    CN = {
        "🇭🇰": "香港", "🇸🇬": "新加坡", "🇹🇼": "台湾", "🇯🇵": "日本", "🇰🇷": "韩国",
        "🇲🇴": "澳门", "🇬🇧": "英国", "🇫🇷": "法国", "🇩🇪": "德国", "🇮🇹": "意大利",
        "🇳🇴": "挪威", "🇺🇸": "美国", "🇨🇦": "加拿大", "🇦🇺": "澳洲", "🇺🇦": "乌克兰",
        "🇹🇷": "土耳其", "🇦🇪": "阿联酋", "🇳🇬": "尼日利亚", "🇵🇭": "菲律宾",
        "🇹🇭": "泰国", "🇻🇳": "越南", "🇲🇾": "马来西亚", "🇮🇳": "印度", "🇮🇩": "印尼",
        "🇦🇷": "阿根廷", "🇧🇷": "巴西", "🇨🇳": "中国", "🇷🇺": "俄罗斯", "🇳🇱": "荷兰",
        "🇪🇸": "西班牙", "🇸🇪": "瑞典", "🇨🇭": "瑞士", "🇵🇱": "波兰", "🇮🇪": "爱尔兰",
        "🇫🇮": "芬兰", "🇩🇰": "丹麦", "🇦🇹": "奥地利", "🇧🇪": "比利时", "🇨🇿": "捷克",
        "🇬🇷": "希腊", "🇮🇱": "以色列", "🇸🇦": "沙特", "🇿🇦": "南非", "🇲🇽": "墨西哥",
        "🇨🇱": "智利", "🇳🇿": "新西兰", "🇵🇰": "巴基斯坦", "🇧🇩": "孟加拉", "🇱🇦": "老挝",
        "🇰🇭": "柬埔寨", "🇲🇲": "缅甸", "🇳🇵": "尼泊尔", "🇱🇰": "斯里兰卡", "🇲🇳": "蒙古",
        "🇰🇿": "哈萨克", "🇪🇬": "埃及", "🇭🇺": "匈牙利", "🇷🇴": "罗马尼亚",
    }
    if flag:
        return flag, CN.get(flag, flag), FLAG_CC.get(flag, "")
    if name in ("DIRECT", "REJECT"):
        return "🌐", name, ""
    return "🌐", "其它", ""


def _ocr_state(app) -> dict[str, Any]:
    """OCR 桥快照。status() 只探一次 /health（1 秒超时），不联网查模型。"""
    from . import ocrbridge
    try:
        st = dict(ocrbridge.status())
    except Exception as e:  # noqa: BLE001 面板不能因为一个可选组件报错
        st = {"ok": False, "reason": "%s: %s" % (type(e).__name__, e)}
    st["enabled"] = bool(app.settings.get("ocr_bridge", True))
    return st


def _doctor_state(app) -> dict[str, Any]:
    """诊断内核的轻量状态。真正的快照由 /api/doctor/status 懒启动后再取。"""
    try:
        st = doctor.light(app)
    except Exception as e:  # noqa: BLE001 诊断本身挂了也不能让面板白屏
        st = {"enabled": True, "available": False, "version": "", "started": False,
              "error": "%s: %s" % (type(e).__name__, e)}
    if not st.get("enabled"):
        st["error"] = st.get("error") or "诊断内核已在设置里关闭（settings.json 的 doctor）"
    return st


def build_state(app) -> dict[str, Any]:
    st = app.snapshot()
    proxies = st.get("proxies") or {}
    delays = st.get("delays") or {}
    cur = st.get("group_now") or ""
    group_types = {"selector", "urltest", "fallback", "loadbalance"}

    nodes: list[dict[str, Any]] = []
    purity_map = st.get("purity") or {}
    speed_map = st.get("speed") or {}
    for n in st.get("group_all") or []:
        meta = proxies.get(n) or {}
        if meta.get("type", "").lower() in group_types:
            continue
        flag, region, cc = _region_of(n)
        pu = purity_map.get(n) or {}
        sp = speed_map.get(n) or {}
        nodes.append({
            "name": n,
            "flag": flag,
            "region": region,
            "cc": cc,
            "delay": delays.get(n),
            "current": n == cur,
            "type": meta.get("type", ""),
            "purity": pu.get("score"),
            "purity_grade": pu.get("grade"),
            "purity_color": pu.get("color"),
            "purity_tags": pu.get("tags") or [],
            "purity_ip": pu.get("ip"),
            # 纯净度是谁给的：one-ip / ipapi.is / ip-api。
            # 前端据此在分数后面标来源，用户能一眼看出这个数是第三方评分还是本地自算的。
            "purity_src": pu.get("src") or "",
            "purity_trust": pu.get("trust"),
            "lat": pu.get("lat"),
            "lon": pu.get("lon"),
            "city": pu.get("city"),
            "mbps": sp.get("mbps"),
        })

    regions: dict[str, dict[str, Any]] = {}
    for nd in nodes:
        key = nd["region"]
        r = regions.setdefault(key, {
            "region": key, "flag": nd["flag"], "cc": nd["cc"],
            "count": 0, "best": None, "best_purity": None,
        })
        r["count"] += 1
        if nd["delay"] and (r["best"] is None or nd["delay"] < r["best"]):
            r["best"] = nd["delay"]
        if nd["purity"] is not None and (r["best_purity"] is None or nd["purity"] > r["best_purity"]):
            r["best_purity"] = nd["purity"]

    s = app.settings
    return {
        "ui": UI_VER,
        "ver": _APP_VER,
        "agreed": str(s.get("agreed_version") or ""),
        "conflict": st.get("conflict") or {},
        "phase": st.get("phase"),
        "kernel_ok": st.get("kernel_ok"),
        "error": st.get("error", ""),
        "busy": st.get("busy", ""),
        "mode": st.get("mode"),
        "sysproxy": st.get("sysproxy"),
        # 网络还没就绪（校园网没过认证 / 断网）时主动不接管系统代理，
        # 前端据此把开关显示成「暂缓」而不是「用户关掉了」
        "sysproxy_hold": bool(st.get("sysproxy_hold")),
        "tun": bool(s.get("tun")),
        "autostart": bool(s.get("autostart")),
        "auto_best": bool(s.get("auto_best")),
        "heal": bool(s.get("heal", True)),
        "current": cur,
        "group": s.get("main_group"),
        "groups": [g for g in proxies if (proxies[g] or {}).get("type", "").lower() in group_types],
        "nodes": nodes,
        "regions": sorted(regions.values(), key=lambda x: -x["count"]),
        "info": st.get("info_nodes") or [],
        "traffic": st.get("traffic") or {},
        "connections": st.get("connections", 0),
        "probing": bool(getattr(app, "_purity_running", False) or getattr(app, "_speed_running", False)),
        # 延迟是「上一次成功测出来的」结果 + 本次刷新的失败原因，
        # 前端据此在列表头标一句「上次刷新失败，显示的是 X 分钟前的数据」
        "delay_ts": _num(getattr(app, "_delays_ts", 0.0)),
        "delay_error": str(st.get("delay_error") or ""),
        "progress": st.get("progress") or {},
        "ai": app.ai_status(),
        # 场景模块：组名、当前出口、成员数，前端直接渲染（2.5.9 起是数据驱动的，
        # 用户自己加的场景也会出现在这里，字段只增不减）
        "scenes": app.scenes_status(),
        # 模块清单（轻量版，给「模块」卡片和编辑入口用）
        "modules": app.modules_status(),
        # AI 接口：开关 / 权限级别 / token 掩码（明文只走 POST /api/ai/api
        # {"action":"reveal"}，见 ai_api 处理函数的注释）
        "ai_api": aiops.state(app),
        # GitHub 特化：最近一次自检结果 + git 全局代理当前指向
        "github": st.get("github") or {},
        "gh_busy": bool(st.get("gh_busy") or getattr(app, "_github_busy", False)),
        "git_proxy": str(st.get("git_proxy") or ""),
        # 本地 DNS 兜底：running 表示端口在手，probe 才是「现在真能解析吗」。
        # 这里只带静态快照，探活得走 /api/dns（会真的发一次查询，有几百毫秒开销）。
        "dns": {
            "running": bool(st.get("dns_running")),
            "reason": str(st.get("dns_reason") or ""),
            "enabled": bool(s.get("dns_guard", True)),
            "port": 53,
        },
        "git_proxy_note": str(st.get("git_proxy_note") or ""),
        # 断网急救：只带缓存的体检结果，真跑要走 /api/net/diagnose
        "netdoc": st.get("netdoc") or {},
        # 异步体检的运行态：前端据此决定显示「体检中…」还是错误提示
        "netdoc_running": bool(st.get("netdoc_running")),
        "netdoc_error": str(st.get("netdoc_error") or ""),
        # 交易通道监控（出口 IP / REST 延迟 / 行情 WS / 交易域名解析）
        "trade": okxguard.status(),
        # 校园网：在线状态 / 认出来的门户 / 上次登录结果（不含密码）
        "campus": app.campus_status(),
        # OCR 桥：端口在听吗、模型、已转张数。探活用 /health，不是 TCP connect。
        "ocr": _ocr_state(app),
        # 诊断内核（yuans_nd）：轻量状态，打开诊断卡才真正启动采集
        "doctor": _doctor_state(app),
        "update": st.get("update") or {},
        "update_repo": s.get("update_repo", ""),
        "purity_count": sum(1 for n in nodes if n.get("purity") is not None),
        "speed_count": sum(1 for n in nodes if n.get("mbps") is not None),
        "sub": {
            "url": s.get("sub_url", ""),
            "last_ok": st.get("sub_last_ok", 0),
            "count": st.get("sub_nodes", 0),
            "error": st.get("sub_error", ""),
            "active": _active(s),
            "mode": app.sub_mode(),
            "total_left_gb": _total_left(app),
            "usable": len(app.sub_pool()),
            "list": [_sub_row(app, i, sub, s) for i, sub in enumerate(app.subscriptions())],
            "now": (app.active_sub().get("info") or {}),
            "name": app.active_sub().get("name") or "",
        },
        "ports": {"proxy": s.get("mixed_port"), "api": s.get("controller_port")},
        "ts": time.time(),
    }


# ────────────────────────── HTTP ──────────────────────────
class _Server(ThreadingHTTPServer):
    """关掉 SO_REUSEADDR。

    Windows 上它允许抢占别人正在监听的端口：bind 照样成功，两个进程各拿
    一半请求 —— 结果就是「旧面板还活着、新面板以为自己起来了」，端口重试
    循环永远走不到 except 分支。关掉之后被占就老实报错，重试才有意义。
    """
    allow_reuse_address = False
    daemon_threads = True


class WebUI:
    def __init__(self, app, on_quit: Callable[[], None], port: int = WEB_PORT_DEFAULT) -> None:
        self.app = app
        self.on_quit = on_quit
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._index = asset("index.html")
        self._log_lines: list[str] = []
        self._route_table: dict[str, Callable[[dict], Any]] | None = None

    # —— 生命周期 ——
    def start(self) -> bool:
        for attempt in range(8):
            try:
                port = self.port + attempt
                handler = self._make_handler()
                self.httpd = _Server(("127.0.0.1", port), handler)
                self.port = port
                break
            except OSError as e:
                log.warning("Web 面板端口 %s 被占用: %s", self.port + attempt, e)
                self.httpd = None
        if not self.httpd:
            log.error("Web 面板启动失败：%s..%s 全被占用", self.port, self.port + 7)
            return False
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="webui", daemon=True)
        self._thread.start()
        log.info("Web 面板: http://127.0.0.1:%s/", self.port)
        return True

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    # —— 后台动作 ——
    def _bg(self, fn, *a, **kw) -> None:
        """后台动作：异常一律落日志。

        以前线程体直接抛，pythonw 下 stderr 是 None，异常连 traceback 都不留，
        面板只会「点了没反应」。这里统一兜住并记到 app.log。
        """
        name = getattr(fn, "__name__", "task")

        def run() -> None:
            try:
                fn(*a, **kw)
            except Exception:  # noqa: BLE001
                log.exception("后台任务 %s 失败", name)

        threading.Thread(target=run, name=f"bg-{name}", daemon=True).start()

    def _table(self) -> dict[str, Callable[[dict], Any]]:
        """路由表只算一次：每个 POST 重建 41 条闭包纯属浪费。"""
        if self._route_table is None:
            self._route_table = self._routes()
        return self._route_table

    def _routes(self) -> dict[str, Callable[[dict], Any]]:
        """路由表：合并下面按功能域拆开的五张子表。

        注意：路径字符串是与 assets/index.html 的硬编码契约，改名等于静默 404；
        合并顺序保持与历史一致（同一批路径的注册先后不变）。
        """
        table: dict[str, Callable[[dict], Any]] = {}
        for part in (self._routes_node, self._routes_sub, self._routes_probe,
                     self._routes_scene, self._routes_misc):
            table.update(part())
        return table

    # ── 节点 / 连接 / 系统开关 ──
    def _routes_node(self) -> dict[str, Callable[[dict], Any]]:
        app = self.app

        def select(body):
            name = (body or {}).get("name", "")
            if not name:
                return {"ok": False, "error": "缺少节点名"}
            self._bg(app.select_node, name)
            return {"ok": True, "queued": name}

        def mode(body):
            app.set_mode((body or {}).get("mode", "rule"))
            return {"ok": True}

        def sysproxy(body):
            on = _flag((body or {}).get("on", True), "on")
            self._bg(app.toggle_system_proxy, on)
            return {"ok": True}

        def autostart(body):
            on = _flag((body or {}).get("on", True), "on")
            self._bg(app.set_autostart, on)
            return {"ok": True}

        def power(body):
            on = _flag((body or {}).get("on", True), "on")
            return app.power(on)

        def best(_):
            self._bg(app.auto_pick_best)
            return {"ok": True, "queued": "auto_best"}

        def test(_):
            self._bg(app.test_delays)
            return {"ok": True, "queued": "delay_test"}

        return {
            "/api/select": select,
            "/api/power": power,
            "/api/mode": mode,
            "/api/sysproxy": sysproxy,
            "/api/autostart": autostart,
            "/api/best": best,
            "/api/test": test,
        }

    # ── 订阅 ──
    def _routes_sub(self) -> dict[str, Callable[[dict], Any]]:
        app = self.app

        def sub(_):
            self._bg(app.update_subscription, True)
            return {"ok": True, "queued": "subscription"}

        def set_sub_url(body):
            b = body or {}
            url = str(b.get("url", "")).strip()
            name = str(b.get("name", "") or "").strip()
            action = str(b.get("action", "add") or "add")
            if action == "use":
                ok, err = app.use_subscription(_int(b.get("index"), 0, 0, 999), keep_node=True)
            elif action == "mode":
                ok, err = app.set_sub_mode(str(b.get("mode", "single")))
            # 前端 1.8.0~2.2.2 发的是 "del"，老的后端只认 "remove"，
            # 于是删除请求掉进下面的 else 分支，被当成「加一条空订阅」，
            # 用户看到的是「订阅链接必须以 http:// 或 https:// 开头」，
            # 日志里连一行都没有 —— 订阅永远删不掉。两个都收。
            elif action in ("remove", "del"):
                ok, err = app.remove_subscription(_int(b.get("index"), 0, 0, 999))
            else:
                if not url:
                    return {"ok": False, "error": "缺少订阅链接"}
                ok, err = app.set_subscription(url, name)
            return {"ok": ok, "error": err}

        def sub_info(_):
            return {
                "ok": True,
                "list": [
                    {
                        "name": s.get("name"), "url": s.get("url"),
                        "nodes": s.get("nodes"), "updated": s.get("updated"),
                        "info": s.get("info") or {},
                    }
                    for s in app.subscriptions()
                ],
                "active": _active(app.settings),
            }

        return {
            "/api/sub": sub,
            "/api/sub_url": set_sub_url,
            "/api/subs": sub_info,
        }

    # ── 纯净度 / 带宽 ──
    def _routes_probe(self) -> dict[str, Callable[[dict], Any]]:
        app = self.app

        def check_purity(body):
            scope = str((body or {}).get("scope", "current"))
            limit = _int((body or {}).get("limit"), 0, 0, 500)
            cc = str((body or {}).get("cc", "") or "").upper()
            if scope == "current":
                cur = app.state.get("group_now")
                if not cur:
                    return {"ok": False, "error": "还没有选中节点"}
                names, limit = [cur], 0
            elif scope == "cc" and cc:
                # 只测地图上选中的那个国家/地区，测完编号点就能按真实 IP 城市落位
                proxies = app.state.get("proxies") or {}
                names = [n for n in (app.state.get("group_all") or [])
                         if _region_of(n)[2] == cc
                         and (proxies.get(n) or {}).get("type", "").lower()
                         not in ("selector", "urltest", "fallback", "loadbalance", "direct", "reject")]
                if not names:
                    return {"ok": False, "error": f"{cc} 没有可测节点"}
                limit = 0
            else:
                names = None
            # scope=all 时连「测速没通过」的节点也算进来（用户点「全部」就是想全测）
            self._bg(app.check_purity, names, limit, scope == "all")
            return {"ok": True, "queued": scope, "count": len(names) if names else 0}

        def check_speed(body):
            limit = _int((body or {}).get("limit"), 10, 1, 500)
            scope = str((body or {}).get("scope", "top"))
            if scope == "current":
                cur = app.state.get("group_now")
                if not cur:
                    return {"ok": False, "error": "还没有选中节点"}
                names = [cur]
            else:
                names = None
            self._bg(app.check_speed, names, 0 if scope == "current" else limit)
            return {"ok": True, "queued": scope}

        return {
            "/api/purity": check_purity,
            "/api/speed": check_speed,
        }

    # ── 场景加速 + AI 专项 + GitHub ──
    def _routes_scene(self) -> dict[str, Callable[[dict], Any]]:
        app = self.app

        def ai_node(body):
            name = str((body or {}).get("name", "")).strip()
            ok, err = app.set_ai_node(name)
            return {"ok": ok, "error": err}

        def ai_toggle(body):
            on = _flag((body or {}).get("on", True), "on")
            ok, err = app.set_ai_enabled(on)
            return {"ok": ok, "error": err}

        def scene_node(body):
            b = body or {}
            ok, err = app.set_scene_node(str(b.get("key", "")).strip(),
                                         str(b.get("name", "")).strip())
            return {"ok": ok, "error": err}

        def scene_toggle(body):
            b = body or {}
            on = b.get("on", True)
            if not isinstance(on, bool):
                return {"ok": False, "error": "on 必须是 true / false"}
            ok, err = app.set_scene_enabled(str(b.get("key", "")).strip(), on)
            return {"ok": ok, "error": err}

        def github_check(_body):
            """GitHub 专项自检：五条链路 + git 实测要 30~40 秒。

            同步跑会把面板 HTTP 线程按住（前端转圈、浏览器还可能先超时），
            所以只排队，结果写 state["github"]，前端轮询 gh_busy 取。
            """
            return app.github_check_bg()

        def git_proxy_fix(_body):
            note = app.sync_git_proxy()
            return {"ok": True, "note": note, "value": str(app.state.get("git_proxy") or "")}

        def ai_fingerprint(_):
            cc = (app.ai_status() or {}).get("exit_cc") or ""
            return {"ok": True, "data": disguise.inspect(cc)}

        def ai_align(_):
            cc = (app.ai_status() or {}).get("exit_cc") or ""
            if not cc:
                return {"ok": False, "error": "还不知道出口国家，先点一次「测出口 IP」"}
            ok, msg, need_admin = disguise.set_timezone_for_ex(cc)
            return {"ok": ok, "message": msg, "cc": cc, "need_admin": need_admin}

        def ai_restore_tz(_):
            ok, msg = disguise.restore_timezone()
            return {"ok": ok, "message": msg}

        def ai_launcher(body):
            tool = str((body or {}).get("tool") or "claude").strip().lower()
            if tool not in ("claude", "codex"):
                return {"ok": False, "error": "未知工具"}
            cc = (app.ai_status() or {}).get("exit_cc") or "US"
            port = _int(app.settings.get("mixed_port"), 7891, 1024, 65535)
            ok, path = disguise.make_launcher(cc, tool, port)
            return {"ok": ok, "path": path, "cc": cc, "message": "" if ok else path}

        def module_get(body):
            key = str((body or {}).get("key") or "").strip().lower()
            mod = app.module_get(key)
            if not mod:
                return {"ok": False, "error": "未知模块：" + key}
            return {"ok": True, "module": mod}

        def module_save(body):
            """新建 / 覆盖模块。面板的编辑框和 AI 接口走的是同一个实现。"""
            b = body or {}
            mod = b.get("module") if isinstance(b.get("module"), dict) else b
            ok, err = app.module_save(mod)
            return {"ok": ok, "error": err, "modules": app.modules_status()}

        def module_delete(body):
            ok, err = app.module_delete(str((body or {}).get("key") or ""))
            return {"ok": ok, "error": err, "modules": app.modules_status()}

        def module_reset(_body):
            ok, err = app.module_reset()
            return {"ok": ok, "error": err, "modules": app.modules_status()}

        def module_import(body):
            b = body or {}
            payload = b.get("modules") if "modules" in b else b.get("json")
            ok, err = app.module_import(payload)
            return {"ok": ok, "error": "" if ok else err, "note": err if ok else "",
                    "modules": app.modules_status()}

        def module_export(_body):
            return {"ok": True, "export": app.module_export()}

        def ai_api(body):
            """AI 接口本身的管理：开关 / 权限上限 / 重置 token / 显式取明文。

            默认只回掩码：/api/state 每秒被轮询一次，还会被插件、录屏、贴日志的
            用户看到，明文 token 等于把这些渠道都变成密钥泄漏面。
            "复制"按钮走 {"action":"reveal"}：这条请求必须是同源页面发的
            application/json（do_POST 已经校验 Host、Origin、Content-Type），
            所以只有用户自己点得到，别的网站拿不到明文。
            """
            b = body or {}
            s = app.settings
            if str(b.get("action") or "").strip().lower() == "reveal":
                return {"ok": True, "ai_api": aiops.state(app, reveal=True)}
            if "enabled" in b:
                s[aiops.ENABLED_KEY] = _flag(b.get("enabled"), "enabled")
            if "level" in b:
                lv = str(b.get("level") or "").strip().lower()
                if lv in aiops.LEVELS:
                    s[aiops.LEVEL_KEY] = lv
            if "regen" in b:
                # 以前是 if b.get("regen")：传 "false" 也会清 token（字符串真值陷阱）
                if _flag(b.get("regen"), "regen"):
                    s[aiops.TOKEN_KEY] = ""
            s.save()
            return {"ok": True, "ai_api": aiops.state(app)}

        def ai_probe(_):
            st = app.ai_status()
            node = st.get("node")
            if not node:
                return {"ok": False, "error": "AI 组还没有选中节点"}
            self._bg(app.check_purity, [node], 0)
            return {"ok": True, "queued": node}

        def campus_save(body):
            return app.campus_save(body or {})

        def campus_login(body):
            return app.campus_login_now(str((body or {}).get("portal") or ""))

        def ocr_status(_body):
            return {"ok": True, "ocr": _ocr_state(app)}

        def ocr_restart(_body):
            from . import ocrbridge
            port = int(app.settings.get("ocr_bridge_port", 8791) or 8791)
            ok, msg = ocrbridge.restart(dir=app.settings.get("ocr_bridge_dir") or None, port=port)
            return {"ok": bool(ok), "message": msg, "ocr": _ocr_state(app)}

        def ocr_config(body):
            """开关 OCR 桥守护。只写 settings，不重建配置（这条链路与代理无关）。"""
            from . import ocrbridge
            b = body or {}
            on = _flag(b.get("enabled"), "enabled")
            app.settings["ocr_bridge"] = on
            try:
                app.settings.save()
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": "保存失败：%s" % e}
            port = int(app.settings.get("ocr_bridge_port", 8791) or 8791)
            msg = ""
            if on:
                ok, msg = ocrbridge.ensure(dir=app.settings.get("ocr_bridge_dir") or None, port=port)
                if not ok:
                    msg = "守护已开，但这次没拉起：%s" % msg
                else:
                    ocrbridge.watch(interval=int(app.settings.get("ocr_bridge_interval", 30) or 30),
                                    dir=app.settings.get("ocr_bridge_dir") or None, port=port,
                                    enabled=lambda: bool(app.settings.get("ocr_bridge", True)))
            else:
                ocrbridge.stop()
                msg = "守护已关，正在跑的 node 不动（识别任务不会被你打断）"
            return {"ok": True, "message": msg, "ocr": _ocr_state(app)}

        def campus_detect(_body):
            """只探测不改配置：给「认一下门户」按钮用。"""
            info = campus.discover()
            if info.get("portal"):
                app.settings["campus_portal"] = info["portal"]
                try:
                    app.settings.save()
                except Exception:  # noqa: BLE001
                    pass
            return {"ok": True, **info, "campus": app.campus_status()}

        # ── 诊断内核（yuans_nd 1.4）：全部走宿主进程内调用，不占额外端口 ──
        def doctor_status(_body=None):
            return {"ok": True, "doctor": doctor.status(app)}

        def doctor_diagnose(body):
            b = body or {}
            mode = str(b.get("mode") or "quick").strip().lower()
            if mode not in ("quick", "deep", "paste"):
                return {"ok": False, "error": "mode 只能是 quick / deep / paste"}
            out = doctor.diagnose(app, mode=mode, text=str(b.get("text") or ""))
            return {"ok": bool(out.get("ok")), **out, "doctor": _doctor_state(app)}

        def doctor_detail(body):
            b = body or {}
            host = str(b.get("host") or "").strip()
            if not host:
                return {"ok": False, "error": "要填主机名，例如 www.okx.com"}
            try:
                port = int(b.get("port") or 443)
            except (TypeError, ValueError):
                port = 443
            out = doctor.detail(app, host, port=port)
            return {"ok": bool(out.get("ok")), **out}

        def doctor_events(body):
            b = body or {}
            try:
                limit = max(1, min(500, int(b.get("limit") or 80)))
            except (TypeError, ValueError):
                limit = 80
            out = doctor.events(app, limit=limit, kind=str(b.get("kind") or ""))
            return {"ok": bool(out.get("ok")), **out}

        def doctor_actions(_body=None):
            return {"ok": True, **doctor.actions(app)}

        def doctor_fix(body):
            """执行一个动作。elevate=true 表示「弹 UAC 让用户自己点」。"""
            b = body or {}
            act_id = str(b.get("action") or "").strip()
            if not act_id:
                return {"ok": False, "error": "缺少 action"}
            why = str(b.get("why") or "用户在诊断页点击")
            if _flag(b.get("elevate", False), "elevate"):
                out = doctor.elevate_run(app, act_id, why=why)
            else:
                out = doctor.fix(app, act_id, why=why, params=b.get("args") or {})
            return {**out, "doctor": _doctor_state(app)}

        def doctor_export(body):
            """把结论 + 证据 + 详细视图 + 事件打成一个 zip，交给用户拿去复盘。"""
            b = body or {}
            host = str(b.get("host") or "").strip()
            try:
                port = int(b.get("port") or 443)
            except (TypeError, ValueError):
                port = 443
            out = doctor.export(app, host, port=port)
            return {**out, "doctor": _doctor_state(app)}

        def doctor_undo(body):
            b = body or {}
            run_id = str(b.get("run_id") or "").strip()
            if not run_id:
                return {"ok": False, "error": "缺少 run_id"}
            out = doctor.undo(app, run_id)
            return {**out, "doctor": _doctor_state(app)}

        return {
            "/api/ai/node": ai_node,
            "/api/ai/toggle": ai_toggle,
            "/api/scene/node": scene_node,
            "/api/scene/toggle": scene_toggle,
            "/api/github/check": github_check,
            "/api/git/proxy": git_proxy_fix,
            "/api/ai/fingerprint": ai_fingerprint,
            "/api/ai/align": ai_align,
            "/api/ai/restore_tz": ai_restore_tz,
            "/api/ai/launcher": ai_launcher,
            "/api/ai/probe": ai_probe,
            "/api/ai/api": ai_api,
            "/api/module/get": module_get,
            "/api/module/save": module_save,
            "/api/module/delete": module_delete,
            "/api/module/reset": module_reset,
            "/api/module/import": module_import,
            "/api/module/export": module_export,
            "/api/campus/save": campus_save,
            "/api/campus/login": campus_login,
            "/api/campus/detect": campus_detect,
            "/api/ocr/status": ocr_status,
            "/api/ocr/restart": ocr_restart,
            "/api/ocr/config": ocr_config,
            "/api/doctor/status": doctor_status,
            "/api/doctor/diagnose": doctor_diagnose,
            "/api/doctor/detail": doctor_detail,
            "/api/doctor/events": doctor_events,
            "/api/doctor/actions": doctor_actions,
            "/api/doctor/fix": doctor_fix,
            "/api/doctor/export": doctor_export,
            "/api/doctor/undo": doctor_undo,
        }

    # ── 杂项：拓扑 / 内核 / 连接 / 更新 / 冲突清理 / 反馈 ──
    def _routes_misc(self) -> dict[str, Callable[[dict], Any]]:
        app = self.app

        def topo(_):
            return {"ok": True, "topo": app.topo()}

        def restart(body):
            b = body or {}
            # 默认排队（前端点一下就回，别让按钮等 3 秒）；需要知道「内核到底起来
            # 没有」的调用方（脚本 / AI / 诊断）传 sync=true 拿真布尔。
            # restart_kernel 现在返回 bool，异步路径不能再把结果丢掉。
            if "sync" in b and _flag(b.get("sync"), "sync"):
                ok = bool(app.restart_kernel())
                return {"ok": ok, "restarted": ok,
                        "error": "" if ok else "内核没起来：看 /api/state 的 error 与 kernel 日志"}
            self._bg(app.restart_kernel)
            return {"ok": True, "queued": "restart"}

        def close_conns(_):
            self._bg(app.close_connections)
            return {"ok": True}

        def auto_best_toggle(body):
            on = _flag((body or {}).get("on", False), "on")
            app.settings["auto_best"] = on
            app.settings.save()
            return {"ok": True, "auto_best": on}

        def heal_toggle(body):
            on = _flag((body or {}).get("on", True), "on")
            app.settings["heal"] = on
            app.settings.save()
            return {"ok": True, "heal": on}

        def dns_state(body):
            """本地 DNS 兜底：看状态 / 探活 / 开关。

            想知道「我这台机器到底能不能解析」的时候，前端只发 {"probe": true}：
            它会真的向 127.0.0.1:53 发一条查询，把毫秒数和失败原因带回来，
            比看「running 字段」准得多 —— 端口被别的软件占着时 running 是 false，
            但解析其实完全正常，这时候不该报警。
            """
            b = body or {}
            guard = dnsguard.ensure(enabled=bool(app.settings.get("dns_guard", True)))
            if "on" in b:
                on = _flag(b.get("on"), "on")
                app.settings["dns_guard"] = on
                app.settings.save()
                if on:
                    guard.start()
                else:
                    guard.stop()
                app.state["dns_running"] = guard.running
                app.state["dns_reason"] = guard.reason
                return {"ok": True, "on": on, "dns": guard.status()}
            out = guard.status()
            out["probe"] = dnsguard.probe(port=guard.port,
                                          timeout=1.5 if b.get("probe") else 0.5)
            return {"ok": True, "dns": out}

        def quit(_):
            self._bg(self._do_quit)
            return {"ok": True}

        def log_tail(_):
            try:
                p = ROOT / "data" / "logs" / "app.log"
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-60:]
                return {"ok": True, "lines": lines}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "lines": [f"读取日志失败: {e}"]}

        def update_check(body):
            force = _flag((body or {}).get("force", False), "force")
            return app.update_check(force=force)

        def update_repo(body):
            return app.update_set_repo(str((body or {}).get("repo") or ""))

        def update_apply(body):
            return app.update_apply(confirm=bool((body or {}).get("confirm")))

        def net_diagnose(body):
            # refresh=false 直接吃缓存：页面切回来不该重跑（一轮 ~6 秒）
            b = body or {}
            refresh = _flag(b.get("refresh", True), "refresh")
            # 同步老行为保留（curl 和 AI 直接要结果），新前端传 async=true 走后台
            if "async" in b and _flag(b.get("async"), "async"):
                return app.net_diagnose_async(refresh=refresh)
            return app.net_diagnose(refresh)

        def net_diagnose_start(body):
            """只起后台体检、立刻返回 running：6 秒级系统调用不该堵住 HTTP。

            结果照旧写 state["netdoc"]，前端轮询 /api/state 的 netdoc /
            netdoc_running / netdoc_error 三个字段即可。
            """
            b = body or {}
            return app.net_diagnose_async(refresh=_flag(b.get("refresh", True), "refresh"))

        def net_repair(body):
            acts = (body or {}).get("actions")
            return app.net_repair([str(a) for a in acts] if isinstance(acts, list) else None)

        def net_fix_one(body):
            return app.net_fix_one(str((body or {}).get("action") or ""))

        def net_report(_body):
            return {"ok": True, "text": app.net_report()}

        def net_scan(_):
            return app.net_scan()

        def net_fix(body):
            b = body or {}
            target = str(b.get("target") or "").strip()

            def _pids(v):
                out = []
                for x in v if isinstance(v, list) else []:
                    try:
                        n = int(x)
                    except (TypeError, ValueError):
                        continue
                    if n > 0:
                        out.append(n)
                return out

            names = [str(x) for x in (b.get("names") or [])
                     if isinstance(x, (str, int)) and str(x).strip()]
            return app.net_fix(target, _pids(b.get("pids")), names[:16])

        def feedback_send(body):
            b = body or {}
            title = str(b.get("title") or "").strip()
            detail = str(b.get("detail") or "").strip()
            email = str(b.get("email") or "").strip()
            if not detail and not title:
                return {"ok": False, "error": "请先写一下遇到的问题"}
            return app.feedback_send(title, detail, email)

        def feedback_open(body):
            return app.feedback_open(str((body or {}).get("dir") or ""))

        def agree(body):
            return app.agree(str((body or {}).get("version") or ""))

        def connections(body):
            b = body or {}
            return app.connections(_int(b.get("limit"), 200, 1, 1000))

        def conn_close(body):
            return app.close_connection(str((body or {}).get("id") or ""))

        def conn_close_all(body):  # noqa: ARG001
            return app.close_all_connections()

        def tun(body):
            b = body or {}
            v = b.get("on")
            if not isinstance(v, bool):
                return {"ok": False, "error": "on 必须是 true / false"}
            return app.set_tun(v)

        return {
            "/api/topo": topo,
            "/api/kernel/restart": restart,
            "/api/connections/close": close_conns,
            "/api/auto_best": auto_best_toggle,
            "/api/heal": heal_toggle,
            "/api/dns": dns_state,
            "/api/quit": quit,
            "/api/log": log_tail,
            "/api/update/check": update_check,
            "/api/update/repo": update_repo,
            "/api/update/apply": update_apply,
            "/api/net/diagnose": net_diagnose,
            "/api/net/diagnose/start": net_diagnose_start,
            "/api/net/repair": net_repair,
            "/api/net/fix": net_fix_one,
            "/api/net/report": net_report,
            "/api/netguard": net_scan,
            "/api/netguard/fix": net_fix,
            "/api/feedback": feedback_send,
            "/api/feedback/open": feedback_open,
            "/api/agree": agree,
            "/api/connections": connections,
            "/api/conn/close": conn_close,
            "/api/conn/clear": conn_close_all,
            "/api/tun": tun,
        }


    def _do_quit(self) -> None:
        try:
            self.on_quit()
        except Exception:  # noqa: BLE001
            log.exception("退出失败")

    # —— handler 工厂（方便拿 self）——
    def _make_handler(self):
        webui = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "VPNTrayUI/1.0"
            # 单连接 30 秒上限：不设 timeout 时，一条半开连接（浏览器预连接、
            # 断网没发 FIN）会把工作线程和句柄一直钉住，面板开久了就没人接了。
            timeout = 30

            def log_message(self, fmt, *args):  # 静音
                log.debug("web %s - %s", self.address_string(), fmt % args)

            # Host 校验，挡 DNS rebinding
            def _host_ok(self) -> bool:
                h = (self.headers.get("Host") or "").strip().lower()
                if not h:
                    return False
                if h.startswith("["):  # [::1]:7892
                    end = h.find("]")
                    name = h[1:end] if end > 0 else ""
                else:
                    name = h.split(":")[0]
                return name in ("127.0.0.1", "localhost", "::1")

            # 跨站请求防护：浏览器跨域只能发 text/plain 等简单请求（不触发预检），
            # 强制 application/json 即可让所有跨站 fetch 被浏览器自身的预检拦下。
            def _origin_ok(self) -> bool:
                origin = (self.headers.get("Origin") or "").strip()
                if not origin:
                    return True  # 同源 POST 不带 Origin，curl 也不带
                p = urlparse(origin)
                if p.scheme != "http":
                    return False
                host = (p.hostname or "").lower()
                return host in ("127.0.0.1", "localhost", "::1")

            def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8") -> None:
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    # 安全头：面板虽然只绑回环，但同源页面 / 本地其它程序都能
                    # 嵌它，禁嵌 + 禁嗅探 + 不外带 Referer 是零成本边界。
                    self.send_header("X-Frame-Options", "DENY")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Security-Policy", _CSP)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    self.close_connection = True

            def _json(self, obj, code: int = 200) -> None:
                try:
                    payload = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode()
                except (TypeError, ValueError):
                    # NaN/Infinity 或不可序列化对象：降级成字符串，绝不把 Infinity 发给前端
                    log.warning("响应不可序列化，已降级: %r", type(obj))
                    payload = json.dumps(obj, ensure_ascii=False, default=str).encode()
                self._send(code, payload)

            def do_GET(self) -> None:  # noqa: N802
                if not self._host_ok():
                    return self._json({"ok": False, "error": "bad host"}, 403)
                path = urlparse(self.path).path
                # AI 接口：给本机 AI 客户端自解释用的清单（需 token）
                if path.startswith("/ai/v1/"):
                    q = parse_qs(urlparse(self.path).query)
                    ok, err = aiops.auth(webui.app, self.headers, (q.get("token") or [""])[0])
                    if not ok:
                        return self._json({"ok": False, "error": err}, 401)
                    if path == "/ai/v1/manifest":
                        return self._json(aiops.manifest(webui.app))
                    return self._json({"ok": False, "error": "unknown ai endpoint"}, 404)
                if path in ("/", "/index.html"):
                    try:
                        html = webui._index.read_bytes()
                    except FileNotFoundError:
                        return self._send(500, b"assets/index.html missing", "text/plain; charset=utf-8")
                    return self._send(200, html, "text/html; charset=utf-8")
                if path == "/api/state":
                    try:
                        return self._json({"ok": True, "state": build_state(webui.app)})
                    except Exception as e:  # noqa: BLE001
                        log.exception("build_state 失败")
                        return self._json({"ok": False,
                                           "error": "state 构建失败",
                                           "kind": type(e).__name__}, 500)
                if path == "/favicon.ico":
                    ico = asset("app.png")
                    if ico.exists():
                        return self._send(200, ico.read_bytes(), "image/png")
                    return self._send(404, b"")
                if path in ("/world_map.js", "/local_map.js"):
                    js = asset(path.lstrip("/"))
                    if js.exists():
                        return self._send(200, js.read_bytes(),
                                          "application/javascript; charset=utf-8")
                    return self._send(404, b"")
                return self._json({"ok": False, "error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                if not self._host_ok():
                    return self._json({"ok": False, "error": "bad host"}, 403)
                if not self._origin_ok():
                    return self._json({"ok": False, "error": "bad origin"}, 403)
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return self._json({"ok": False, "error": "need application/json"}, 415)
                path = urlparse(self.path).path
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "bad length"}, 400)
                if n < 0 or n > (1 << 20):
                    return self._json({"ok": False, "error": "bad length"}, 400)
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._json({"ok": False, "error": "bad json"}, 400)
                if not isinstance(body, dict):
                    return self._json({"ok": False, "error": "body must be object"}, 400)
                # AI 调用：{tool, args} 一股脑交给 aiops（权限/参数校验都在里面）
                if path == "/ai/v1/call":
                    q = parse_qs(urlparse(self.path).query)
                    ok, err = aiops.auth(webui.app, self.headers, (q.get("token") or [""])[0])
                    if not ok:
                        return self._json({"ok": False, "error": err}, 401)
                    return self._json(aiops.call(webui.app,
                                                 str(body.get("tool") or ""),
                                                 body.get("args")))
                fn = webui._table().get(path)
                if not fn:
                    return self._json({"ok": False, "error": "unknown endpoint"}, 404)
                try:
                    return self._json(fn(body))
                except BadArg as e:
                    # 入参收敛失败属于调用方问题，不是内部故障：400 + 说清哪个字段
                    return self._json({"ok": False, "error": str(e), "kind": "bad_arg"}, 400)
                except Exception as e:  # noqa: BLE001
                    log.exception("API %s 失败", path)
                    return self._json({"ok": False,
                                       "error": "内部错误",
                                       "kind": type(e).__name__}, 500)

        return Handler
