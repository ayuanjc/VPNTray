"""最小自动化测试：不联网、不起内核，几百毫秒跑完。

跑法（仓库根目录）：
    python -m pytest tests -q
    python tests/test_core.py          # 不装 pytest 也能跑
覆盖的是「改坏了会静默失效」的地方：端口收敛、日志轮转、线程隔离的 Session、
订阅体积上限、路由表缓存、shutdown/更新的幂等与互斥。
"""
from __future__ import annotations

import http.server
import pathlib
import socketserver
import sys
import threading
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vpntray import configgen, kernel, purity, subscription, webui  # noqa: E402
from vpntray.api import MihomoAPI  # noqa: E402
from vpntray.app import App  # noqa: E402
from vpntray.settings import port_setting  # noqa: E402


# ───────── 端口 / 配置 ─────────
def test_port_setting_clamps():
    assert port_setting({"mixed_port": "7891"}, "mixed_port", 1) == 7891
    assert port_setting({"mixed_port": "abc"}, "mixed_port", 7891) == 7891
    assert port_setting({}, "mixed_port", 7891) == 7891
    assert port_setting({"mixed_port": 80}, "mixed_port", 7891) == 7891     # 低于 1024
    assert port_setting({"mixed_port": 70000}, "mixed_port", 7891) == 7891  # 越上界


def test_configgen_minimal():
    sub = {"proxies": [{"name": "HK-1", "type": "ss", "server": "1.2.3.4", "port": 443,
                        "cipher": "aes-128-gcm", "password": "x"}],
           "proxy-groups": [{"name": "节点选择", "type": "select", "proxies": ["HK-1"]}],
           "rules": ["MATCH,DIRECT"], "info": [], "providers": {}}
    cfg = configgen.build(sub, {"mixed_port": 7891, "controller_port": 9091,
                                "ai_enabled": False, "steam_enabled": False,
                                "github_enabled": False}, {"HK-1": 30})
    assert any(g.get("name") for g in cfg["proxy-groups"])
    assert cfg["rules"][-1].startswith("MATCH,")
    assert cfg["mixed-port"] == 7891


def test_purity_score_range():
    info = {"ip": "1.2.3.4", "country": "JP", "asn": {"org": "Example"},
            "company": {"name": "Example", "type": "isp"}}
    r = purity.score(info, "🇯🇵|日本-01")
    assert 0 <= r["score"] <= 100 and r["grade"] and isinstance(r["tags"], list)
    tor = dict(info, is_tor=True)
    assert purity.score(tor, "x")["score"] < r["score"]


# ───────── 内核：日志轮转 ─────────
def test_kernel_log_rotate(monkeypatch, tmp_path):
    log_path = tmp_path / "kernel.log"
    monkeypatch.setattr(kernel, "LOGS", tmp_path)
    monkeypatch.setattr(kernel, "KERNEL_LOG", log_path)
    monkeypatch.setattr(kernel, "KERNEL_LOG_MAX", 32)

    log_path.write_bytes(b"x" * 64)
    kernel._rotate_kernel_log()
    assert (tmp_path / "kernel.log.1").exists() and not log_path.exists()

    log_path.write_bytes(b"y" * 8)          # 没超限就不动
    kernel._rotate_kernel_log()
    assert log_path.read_bytes() == b"y" * 8


# ───────── API 客户端线程隔离 ─────────
def test_api_session_per_thread():
    api = MihomoAPI()
    main = api._session()
    assert api._session() is main
    seen = {}

    def work():
        seen["s"] = api._session()

    t = threading.Thread(target=work)
    t.start(); t.join()
    assert seen["s"] is not main
    assert main.trust_env is False and seen["s"].trust_env is False


# ───────── 订阅体积上限 ─────────
class _BigHandler(http.server.BaseHTTPRequestHandler):
    mode = "big"

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        try:
            if _BigHandler.mode == "declared":
                self.send_response(200)
                self.send_header("Content-Length", str(64 * 1024 * 1024))
                self.end_headers()
                self.wfile.write(b"a" * 16)
                return
            if _BigHandler.mode == "big":
                self.send_response(200)
                self.end_headers()
                for _ in range(40):
                    self.wfile.write(b"a" * 65536)
                return
            body = b"proxies: []\n" + b"#" * 200
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def test_subscription_size_cap(monkeypatch):
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _BigHandler)
    srv.daemon_threads = True
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(subscription, "MAX_SUB_BYTES", 256 * 1024)
    try:
        _BigHandler.mode = "big"
        try:
            subscription.fetch(f"http://127.0.0.1:{port}/big", "ua", 0, timeout=10)
            raise AssertionError("超限的订阅没被拒")
        except subscription.SubscriptionError as e:
            assert "订阅过大" in str(e)

        _BigHandler.mode = "declared"        # 先看 Content-Length，不读 body
        try:
            subscription.fetch(f"http://127.0.0.1:{port}/x", "ua", 0, timeout=10)
            raise AssertionError("Content-Length 超限没被拒")
        except subscription.SubscriptionError as e:
            assert "订阅过大" in str(e)

        _BigHandler.mode = "small"           # 正常订阅照旧
        assert subscription.fetch(f"http://127.0.0.1:{port}/s", "ua", 0, timeout=10).startswith(b"proxies")
    finally:
        srv.shutdown()


# ───────── 面板：路由表缓存 + 异步自检 ─────────
class _StubApp:
    def __init__(self):
        self.settings = {"github_heal": True}
        self.state = {"github": {}, "gh_busy": False}
        self._gh_lock = threading.Lock()
        self._github_busy = False
        self._threads = []
        self._lock = threading.RLock()
        self.on_change = None
        self.calls = 0

    def github_check(self, heal=True):
        self.calls += 1
        time.sleep(0.15)
        res = {"ok": True, "items": [], "ts": time.time()}
        self.state["github"] = res
        return res


def test_webui_routes_cached():
    w = webui.WebUI(_StubApp(), on_quit=None)
    assert w._table() is w._table()
    assert len(w._table()) == 41
    assert "github/check" in " ".join(w._table())


def test_github_check_async():
    st = _StubApp()
    st._spawn = lambda name, fn: threading.Thread(target=fn, daemon=True).start()
    st._set = lambda **kw: st.state.update(kw)
    t0 = time.time()
    r = App.github_check_bg(st)              # 借用真实实现，但 app 是 stub
    assert r["ok"] is True and (time.time() - t0) < 0.2
    assert st.state["gh_busy"] is True
    assert App.github_check_bg(st)["ok"] is False    # 互斥
    time.sleep(0.4)
    assert st.state["gh_busy"] is False and st.calls == 1 and st.state["github"]["ts"]


# ───────── 生命周期幂等 ─────────
def _bare_app():
    app = object.__new__(App)            # 不跑 __init__：不碰真实 data 目录
    app.settings = {"system_proxy": False}
    app.state = {"update": {"has_update": True, "zip_url": "http://x/y.zip", "latest": "1.0"}}
    app._lock = threading.RLock()
    app._stop = threading.Event()
    app._threads = []
    app._shutting = False
    app._update_busy = False
    app.kernel = types.SimpleNamespace(stops=0, stop=lambda: None)
    app.kernel.stop = lambda: setattr(app.kernel, "stops", app.kernel.stops + 1)
    app.on_change = None
    app.on_notify = None
    app._set = lambda **kw: app.state.update(kw)
    app._changed = lambda: None
    app._set_progress = lambda *a, **k: None
    app._spawn = lambda name, fn: None
    app.notify = lambda *a, **k: None
    return app


def test_shutdown_idempotent():
    app = _bare_app()
    app.shutdown(); app.shutdown()
    assert app.kernel.stops == 1


def test_update_apply_single_flight():
    app = _bare_app()
    assert app.update_apply()["ok"] is True
    assert "已经在更新中" in app.update_apply()["error"]


if __name__ == "__main__":
    fails = []
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            if fn.__code__.co_argcount == 0:
                fn()
            else:                      # 需要 monkeypatch/tmp_path 的交给 pytest
                print(f"SKIP {name}（需要 pytest）")
                continue
            print("PASS", name)
        except Exception as e:  # noqa: BLE001
            print("FAIL", name, "->", type(e).__name__, e)
            fails.append(name)
    print("FAILS:", fails or "无")
    sys.exit(1 if fails else 0)
