"""原生窗口控制台。

把已经做好的界面装进一个真正的窗口里，不再往系统浏览器塞标签页。

为什么要单独一个进程：主进程的主线程被 pywin32 的托盘消息泵占着，
pywebview 在 Windows 上又必须拿到主线程（WinForms 的 STA + 消息泵），
两者抢不了同一个线程。所以窗口跑在自己的进程里，靠本地 HTTP 和后台通信 ——
界面本身一行都不用改，后台也完全不知道窗口长什么样。

用法：
    VPNTray.exe --console --port 7892
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import paths

log = logging.getLogger("ui")

TITLE_PREFIX = "VPN Tray 控制台"
GEO_FILE = paths.DATA / "window.json"
DEFAULT_GEO = {"w": 1400, "h": 920}


def _load_geo() -> dict[str, int]:
    """上次的窗口大小。读坏了就当没存过。"""
    try:
        d = json.loads(GEO_FILE.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return dict(DEFAULT_GEO)
        out = {}
        for k in ("w", "h"):
            v = d.get(k)
            if isinstance(v, (int, float)) and 800 <= v <= 5000:
                out[k] = int(v)
        return {**DEFAULT_GEO, **out}
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_GEO)


def _save_geo(w: int, h: int) -> None:
    try:
        paths.ensure_dirs()
        GEO_FILE.write_text(json.dumps({"w": int(w), "h": int(h)}), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.debug("窗口尺寸没存上: %s", e)


def backend_alive(port: int, timeout: float = 2.5) -> bool:
    """后台进程还活着吗。窗口先确认再加载，否则用户会看到一个连不上的白屏。"""
    try:
        # 走直连，别被系统代理绕一圈
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def available() -> tuple[bool, str]:
    """pywebview + WebView2 能不能用。"""
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"没装 pywebview（{type(e).__name__}）"
    return True, ""


def focus() -> bool:
    """已经有窗口就把那个窗口拎到前台，别开第二个。"""
    try:
        u32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _lparam):  # noqa: ANN001
            if not u32.IsWindowVisible(hwnd):
                return True
            n = u32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(TITLE_PREFIX):
                found.append(hwnd)
                return False
            return True

        u32.EnumWindows(_cb, None)
        if not found:
            return False
        hwnd = found[0]
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


def spawn(port: int = 7892) -> bool:
    """另起一个窗口进程。

    窗口不能和后台同进程：后台主线程被托盘的 Win32 消息泵占着，
    pywebview 又必须要主线程起 WinForms 消息泵，两者抢不了同一个线程。
    """
    try:
        root = Path(__file__).resolve().parent.parent
        if getattr(sys, "frozen", False):
            exe, cmd = sys.executable, [sys.executable, "--window", "--port", str(port)]
        else:
            exe = sys.executable or "pythonw.exe"
            if exe.lower().endswith("python.exe"):
                alt = str(Path(exe).with_name("pythonw.exe"))
                if Path(alt).is_file():
                    exe = alt
            cmd = [exe, str(root / "run.pyw"), "--window", "--port", str(port)]
        subprocess.Popen(
            cmd,
            cwd=str(root),
            close_fds=True,
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("开窗口进程失败: %s", e)
        return False


def open_console(port: int = 7892) -> str:
    """优先原生窗口；实在不行退回浏览器。返回实际用的方式。"""
    if focus():
        return "focus"
    ok, _why = available()
    if ok and spawn(port):
        return "window"
    try:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{port}/")
        return "browser"
    except Exception:  # noqa: BLE001
        return "none"


def run(port: int = 7892, title: str = TITLE_PREFIX) -> int:
    """开窗口并阻塞到用户关掉它。返回进程退出码。"""
    ok, why = available()
    if not ok:
        log.error("原生窗口不可用: %s", why)
        return 2

    import webview

    if not backend_alive(port):
        log.error("后台没在 127.0.0.1:%s 上响应", port)

    geo = _load_geo()
    # 窗口标题带上版本，用户截图反馈时能看出是哪个版本
    try:
        from . import __version__ as _v
        title = f"{title} v{_v}"
    except Exception:  # noqa: BLE001
        pass

    win = webview.create_window(
        title,
        f"http://127.0.0.1:{port}/",
        width=geo["w"],
        height=geo["h"],
        min_size=(1000, 680),
        background_color="#0b0f14",
        text_select=True,
        # 关掉右键菜单里的「重新加载/检查」，普通用户用不到那些
        easy_drag=False,
    )

    def _remember() -> None:
        try:
            _save_geo(win.width, win.height)
        except Exception:  # noqa: BLE001
            pass

    try:
        win.events.closed += _remember
    except Exception:  # noqa: BLE001
        pass

    # edgechromium = WebView2，Win11 自带，比 mshtml 快一个数量级
    webview.start(gui="edgechromium", debug=bool(os.environ.get("VPNTRAY_UI_DEBUG")))
    return 0


def main(argv: list[str]) -> int:
    port = 7892
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
    return run(port)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
