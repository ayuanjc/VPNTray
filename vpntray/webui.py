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
from urllib.parse import urlparse

from . import __version__ as _APP_VER
from . import disguise
from .paths import ROOT, asset
from .purity import FLAG_CC

log = logging.getLogger("webui")

WEB_PORT_DEFAULT = 7892
# 前端界面版本；assets/index.html 里的 UI_VER 必须一致，不一致前端会自动刷新
UI_VER = "2.2.2"


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
        "progress": st.get("progress") or {},
        "ai": app.ai_status(),
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
        threading.Thread(target=fn, args=a, kwargs=kw, daemon=True).start()

    def _routes(self) -> dict[str, Callable[[dict], Any]]:
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
            on = bool((body or {}).get("on", True))
            self._bg(app.toggle_system_proxy, on)
            return {"ok": True}

        def autostart(body):
            on = bool((body or {}).get("on", True))
            self._bg(app.set_autostart, on)
            return {"ok": True}

        def best(_):
            self._bg(app.auto_pick_best)
            return {"ok": True, "queued": "auto_best"}

        def test(_):
            self._bg(app.test_delays)
            return {"ok": True, "queued": "delay_test"}

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
            elif action == "remove":
                ok, err = app.remove_subscription(_int(b.get("index"), 0, 0, 999))
            else:
                ok, err = app.set_subscription(url, name)
            return {"ok": ok, "error": err}

        def sub_info(_):
            return {
                "ok": True,
                "list": [
                    {
                        "name": sub.get("name"), "url": sub.get("url"),
                        "nodes": sub.get("nodes"), "updated": sub.get("updated"),
                        "info": sub.get("info") or {},
                    }
                    for sub in app.subscriptions()
                ],
                "active": _active(app.settings),
            }

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

        # ── AI 专项（Claude Code / Codex）──
        def ai_node(body):
            name = str((body or {}).get("name", "")).strip()
            ok, err = app.set_ai_node(name)
            return {"ok": ok, "error": err}

        def ai_toggle(body):
            on = bool((body or {}).get("on", True))
            ok, err = app.set_ai_enabled(on)
            return {"ok": ok, "error": err}

        def topo(_):
            return {"ok": True, "topo": app.topo()}

        def ai_fingerprint(_):
            cc = (app.ai_status() or {}).get("exit_cc") or ""
            return {"ok": True, "data": disguise.inspect(cc)}

        def ai_align(_):
            cc = (app.ai_status() or {}).get("exit_cc") or ""
            if not cc:
                return {"ok": False, "error": "还不知道出口国家，先点一次「测出口 IP」"}
            ok, msg = disguise.set_timezone_for(cc)
            return {"ok": ok, "message": msg, "cc": cc}

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

        def ai_probe(_):
            st = app.ai_status()
            node = st.get("node")
            if not node:
                return {"ok": False, "error": "AI 组还没有选中节点"}
            self._bg(app.check_purity, [node], 0)
            return {"ok": True, "queued": node}

        def restart(_):
            self._bg(app.restart_kernel)
            return {"ok": True, "queued": "restart"}

        def close_conns(_):
            self._bg(app.close_connections)
            return {"ok": True}

        def auto_best_toggle(body):
            on = bool((body or {}).get("on", False))
            app.settings["auto_best"] = on
            app.settings.save()
            return {"ok": True, "auto_best": on}

        def heal_toggle(body):
            on = bool((body or {}).get("on", True))
            app.settings["heal"] = on
            app.settings.save()
            return {"ok": True, "heal": on}

        def power(body):
            on = bool((body or {}).get("on", True))
            return app.power(on)

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

        # ── 自动更新 ──
        def update_check(body):
            force = bool((body or {}).get("force"))
            return app.update_check(force=force)

        def update_apply(_):
            return app.update_apply()

        # ── 代理冲突清理 ──
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

        # ── 反馈 / 用户协议 ──
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

        # ── 实时连接 / TUN ──
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
            "/api/select": select,
            "/api/power": power,
            "/api/mode": mode,
            "/api/sysproxy": sysproxy,
            "/api/autostart": autostart,
            "/api/best": best,
            "/api/test": test,
            "/api/sub": sub,
            "/api/sub_url": set_sub_url,
            "/api/subs": sub_info,
            "/api/purity": check_purity,
            "/api/speed": check_speed,
            "/api/ai/node": ai_node,
            "/api/ai/toggle": ai_toggle,
            "/api/ai/fingerprint": ai_fingerprint,
            "/api/ai/align": ai_align,
            "/api/ai/restore_tz": ai_restore_tz,
            "/api/ai/launcher": ai_launcher,
            "/api/ai/probe": ai_probe,
            "/api/topo": topo,
            "/api/kernel/restart": restart,
            "/api/connections/close": close_conns,
            "/api/auto_best": auto_best_toggle,
            "/api/heal": heal_toggle,
            "/api/quit": quit,
            "/api/log": log_tail,
            "/api/update/check": update_check,
            "/api/update/apply": update_apply,
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
                fn = webui._routes().get(path)
                if not fn:
                    return self._json({"ok": False, "error": "unknown endpoint"}, 404)
                try:
                    return self._json(fn(body))
                except Exception as e:  # noqa: BLE001
                    log.exception("API %s 失败", path)
                    return self._json({"ok": False,
                                       "error": "内部错误",
                                       "kind": type(e).__name__}, 500)

        return Handler
