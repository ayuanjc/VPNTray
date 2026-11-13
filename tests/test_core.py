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
    # 2.5.9 起了 7 条新路由（/api/ai/api + /api/module/*），所以不写死总数，
    # 只守住「表是缓存的、而且不是空的」这件事
    assert len(w._table()) >= 41
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




# ───────── 路径：数据目录可写性 / 回退 ─────────
def test_probe_writable_true_and_creates_dir():
    import tempfile
    from vpntray import paths
    d = pathlib.Path(tempfile.mkdtemp()) / "a" / "b"
    ok, why = paths._probe_writable(d)
    assert ok is True and why == "" and d.is_dir()


def test_probe_writable_false_when_parent_is_file():
    import tempfile
    from vpntray import paths
    f = pathlib.Path(tempfile.mkdtemp()) / "blocker"
    f.write_text("x", encoding="utf-8")
    ok, why = paths._probe_writable(f / "sub")
    assert ok is False and why            # 建不出目录时必须带回原因，不能静默


def test_ensure_dirs_returns_failure_list():
    from vpntray import paths
    fails = paths.ensure_dirs()
    assert isinstance(fails, list)         # 空列表 = 全部就绪；以前返回 None


def test_fallback_state_shape():
    from vpntray import paths
    st_ = paths.fallback_state()
    assert set(st_) == {"data_dir", "data_dir_fallback", "data_dir_reason"}
    assert isinstance(st_["data_dir_fallback"], bool) and st_["data_dir"]


# ───────── settings：类型校验 / ACL ─────────
def test_coerce_rejects_bad_types():
    from vpntray import settings as S
    assert S._coerce(7891, "7891") == (7891, True)     # 数字串可以救回来
    assert S._coerce(7891, None) == (7891, False)      # null 回落默认
    assert S._coerce(False, 0) == (False, True)        # 0/1 视为 bool
    assert S._coerce(False, "maybe") == (False, False)


def test_deep_merge_falls_back_on_type_mismatch():
    from vpntray import settings as S
    fixed: list = []
    base = {"mixed_port": 7891, "subs": "url", "tun": False, "selected": {}}
    merged = S._deep_merge(base, {"mixed_port": None, "subs": 123, "tun": "yes"}, fixed)
    assert merged["mixed_port"] == 7891 and "mixed_port" in fixed
    assert merged["subs"] == "url" and "subs" in fixed      # 整数覆盖不掉字符串
    assert merged["tun"] is False and "tun" in fixed


def test_harden_acl_never_raises():
    from vpntray import settings as S
    S._harden_acl(pathlib.Path("不存在的文件.json"))   # 失败只能 warning，不能抛
    assert isinstance(S._current_user(), str)


# ───────── logger：handler 识别 / 轮转自愈 ─────────
def test_logger_setup_keeps_single_own_handler():
    from vpntray import logger
    r1 = logger.setup()
    r2 = logger.setup()
    ours = [h for h in r2.handlers if getattr(h, "_vpntray_handler", False)]
    assert r1 is r2 and len(ours) == 1    # 不再「root 有 handler 就早退」


def test_logger_emit_reopens_closed_stream():
    import logging
    import tempfile
    from vpntray import logger
    h = logger._SafeRotating(pathlib.Path(tempfile.mkdtemp()) / "x.log",
                            maxBytes=1, backupCount=1, encoding="utf-8")
    try:
        h.stream = None                   # 模拟 doRollover 失败后流被置空
        h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, "hi", None, None))
        assert h.stream is not None       # emit 必须自己把流重开
    finally:
        h.close()


# ───────── cli：安全取值 / down 确认 ─────────
def test_cli_safe_int():
    from vpntray import cli
    assert cli._safe_int("7891", 1) == 7891
    assert cli._safe_int(None, 7) == 7
    assert cli._safe_int("abc", 7) == 7


def test_cli_down_refuses_without_yes():
    from vpntray import cli
    # 非交互 stdin 下必须拒绝：以前会直接还原系统代理并把内核杀掉
    assert cli.main(["down"]) == 2


# ───────── 托盘 / 原生窗口：导入兜底 ─────────
def test_tray_raises_without_pystray():
    from vpntray import tray
    assert hasattr(tray, "IMPORT_ERROR")
    old = tray.pystray
    if old is None:
        return
    tray.pystray = None
    try:
        try:
            tray.Tray(types.SimpleNamespace())
        except RuntimeError as e:
            assert "pystray" in str(e)
        else:
            raise AssertionError("没有 pystray 时应该抛 RuntimeError")
    finally:
        tray.pystray = old


def test_tray_win_guard_matches_platform():
    from vpntray import tray_win
    assert hasattr(tray_win, "IMPORT_ERROR")
    if tray_win.win32gui is not None:
        return
    try:
        tray_win.TrayWin(None, None)
    except RuntimeError as e:
        assert "pywin32" in str(e)
    else:
        raise AssertionError("缺 pywin32 时应该抛 RuntimeError")


def test_ui_native_spawn_result_is_two_tuple():
    from vpntray import ui_native
    r = ui_native.SpawnResult(True, "x", pending=True, proc=None)
    ok, msg = r                            # 调用方按二元组解包必须继续可用
    assert ok is True and msg == "x"
    assert r.pending is True and r.proc is None
    assert ui_native.pending_processes() == []
    assert isinstance(ui_native.webview2_version(), str)


def test_backup_bad_keeps_original_file():
    import tempfile
    from vpntray import settings as S
    old = S.SETTINGS
    d = pathlib.Path(tempfile.mkdtemp())
    S.SETTINGS = d / "settings.json"
    try:
        inst = S.Settings.__new__(S.Settings)   # 不跑 __init__，免得碰真实数据目录
        inst._backup_bad(b'{"subs": 123}', "类型不对")
        got = list(d.glob("settings.json.bad-*"))
        assert len(got) == 1
        assert got[0].read_bytes() == b'{"subs": 123}'   # 原值必须可回查
    finally:
        S.SETTINGS = old


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
