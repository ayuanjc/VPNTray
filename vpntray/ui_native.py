"""原生窗口控制台。

把已经做好的界面装进一个真正的窗口里，不再往系统浏览器塞标签页。

为什么要单独一个进程：主进程的主线程被 pywin32 的托盘消息泵占着，
pywebview 在 Windows 上又必须拿到主线程（WinForms 的 STA + 消息泵），
两者抢不了同一个线程。所以窗口跑在自己的进程里，靠本地 HTTP 和后台通信 ——
界面本身一行都不用改，后台也完全不知道窗口长什么样。

用法：
    VPNTray.exe --window --port 7892

MOTW 自愈
---------
从浏览器下载的 zip 解压后，每个文件会带一个 `Zone.Identifier` 备用数据流（俗称
「来自 Internet」标记）。.NET Framework 在 Assembly.LoadFrom 时**拒绝**加载带这个
标记的程序集（HRESULT 0x80131515），于是 pythonnet 初始化失败、WebView2 程序集
加载不出来、窗口进程秒退。用户看到的就是「点开控制台没反应」。

所以这里在启动窗口前先把整个程序目录的标记清掉。清标记是安全且可逆的（只是去掉
Windows 给文件贴的来源标签），比让用户自己去右键属性勾「解除锁定」现实得多。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths

log = logging.getLogger("ui")

TITLE_PREFIX = "VPN Tray 控制台"
# 任务栏分组用的标识。整个产品统一这一个，主进程和窗口进程都要设成一样，
# 否则两边各算一个应用，图标、跳转列表都会错乱。
APP_ID = "ayuanjc.VPNTray.Desktop"
GEO_FILE = paths.DATA / "window.json"
DEFAULT_GEO = {"w": 1400, "h": 920}
# 窗口从 create Popen 到真的出现，实测 1.2–2.5 秒（WebView2 首次加载更久）
SPAWN_WAIT = 6.0


def _app_root() -> Path:
    """程序根目录：冻结后是 exe 所在目录，源码模式是仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _zone_stream_path(p: Path) -> str:
    return f"{p}:Zone.Identifier"


def has_motw(p: Path) -> bool:
    try:
        return os.path.exists(_zone_stream_path(p))
    except OSError:
        return False


def unblock_tree(root: Path | None = None, limit: int = 4000) -> int:
    """清掉程序目录里所有文件的「来自 Internet」标记。返回清掉的数量。

    不用 subprocess 调 PowerShell（那要起一个进程、还可能被策略拦），
    直接删那个备用数据流：`path:Zone.Identifier` 在 Win32 层就是一个可删的文件名。
    """
    root = root or _app_root()
    removed = 0
    scanned = 0
    try:
        for p in root.rglob("*"):
            if scanned >= limit:
                log.warning("扫描 %d 个文件后停止清理 MOTW（目录过大）", limit)
                break
            if not p.is_file():
                continue
            scanned += 1
            if not has_motw(p):
                continue
            try:
                os.remove(_zone_stream_path(p))
                removed += 1
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        log.warning("清理 MOTW 时出错（已清理 %d 个）: %s", removed, e)
    if removed:
        log.info("已清除 %d 个文件的「来自 Internet」标记（共扫描 %d 个）", removed, scanned)
    return removed


def _fatal(text: str, title: str = "VPN Tray") -> None:
    """窗口起不来时必须让用户看见，不能静默。"""
    log.error("%s", text.replace("\n", " | "))
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:  # noqa: BLE001
        pass


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
    """pywebview 能不能 import。

    注意：import 成功不等于能起窗口 —— 冻结版里 webview 包在但 pythonnet 的
    程序集可能加载失败（MOTW / 缺 DLL）。真正的判断只能靠 spawn() 之后验活。
    """
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"没装 pywebview（{type(e).__name__}: {e}）"
    return True, ""


def _find_console_window() -> int:
    """枚举顶层窗口，找标题以 TITLE_PREFIX 开头的那个。返回 hwnd 或 0。"""
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
        return found[0] if found else 0
    except Exception:  # noqa: BLE001
        return 0


def focus() -> bool:
    """已经有窗口就把那个窗口拎到前台，别开第二个。"""
    hwnd = _find_console_window()
    if not hwnd:
        return False
    try:
        u32 = ctypes.windll.user32
        if u32.IsIconic(hwnd):
            u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001
        return False


def spawn(port: int = 7892, wait: float = SPAWN_WAIT) -> tuple[bool, str]:
    """另起一个窗口进程，并**确认它真的活下来了**。

    只判 Popen 不抛异常是不够的：子进程可能因为缺 DLL / MOTW / 没装 WebView2
    在一秒内退出，父进程却以为「窗口已经打开」，于是既不回退浏览器也不报错。
    这就是 2.0.0 里「点开控制台没反应」的直接原因。
    """
    root = _app_root()
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            cmd = [exe, "--window", "--port", str(port)]
        else:
            exe = sys.executable or "pythonw.exe"
            if exe.lower().endswith("python.exe"):
                alt = str(Path(exe).with_name("pythonw.exe"))
                if Path(alt).is_file():
                    exe = alt
            cmd = [exe, str(root / "run.pyw"), "--window", "--port", str(port)]
    except Exception as e:  # noqa: BLE001
        return False, f"拼启动命令失败: {e}"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            close_fds=True,
            creationflags=0x00000008,  # DETACHED_PROCESS
        )
    except Exception as e:  # noqa: BLE001
        return False, f"起子进程失败: {e}"

    deadline = time.time() + max(1.0, wait)
    while time.time() < deadline:
        time.sleep(0.25)
        if _find_console_window():
            return True, ""
        rc = proc.poll()
        if rc is not None:
            # 秒退：多半是程序集加载失败，把退出码带回去便于排查
            return False, f"窗口进程立即退出（exit={rc}）"
    # 超时但进程还活着：慢机器上首次加载 WebView2 可能要几秒，先算成功
    if proc.poll() is None:
        return True, "（启动较慢）"
    return False, f"窗口进程退出（exit={proc.returncode}）"


def open_console(port: int = 7892) -> str:
    """优先原生窗口；起不来就退回浏览器，绝不静默。返回实际用的方式。"""
    if focus():
        return "focus"

    ok, why = available()
    if ok:
        good, err = spawn(port)
        if good:
            return "window"
        log.warning("原生窗口起不来：%s", err)
        # 大概率是 MOTW 拦住了程序集，清一次再试
        if unblock_tree():
            log.info("已清理 MOTW，重试一次")
            good, err = spawn(port)
            if good:
                return "window"
            log.warning("清 MOTW 后仍然起不来：%s", err)
    else:
        log.warning("pywebview 不可用：%s", why)

    try:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{port}/")
        return "browser"
    except Exception as e:  # noqa: BLE001
        log.error("连浏览器都打不开: %s", e)
        return "none"


def _set_app_id() -> None:
    """给进程一个显式 AppUserModelID。

    不设的话 Windows 任务栏按「exe 路径」分组，dev 下就是 pythonw.exe 的图标
    （那个 Python 蛇形 logo），窗口自己设的 WM_SETICON 管不到任务栏按钮。
    设了 AppID 之后任务栏才按窗口图标走。
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            ctypes.c_wchar_p(APP_ID))
    except Exception:  # noqa: BLE001
        pass


def _force_icon(hwnd: int, ico_path: str) -> None:
    """窗口显示后再钉一次 WM_SETICON（大/小两个都设）。

    pywebview 设的是 Form.Icon，在部分 Windows 版本上任务栏按钮仍会回落到
    exe 图标；这里直接对 hwnd 补一刀，确保 ICON_BIG 是我们那张。
    """
    try:
        LR_LOADFROMFILE = 0x0010
        IMAGE_ICON = 1
        big = ctypes.windll.user32.LoadImageW(
            None, ctypes.c_wchar_p(ico_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        small = ctypes.windll.user32.LoadImageW(
            None, ctypes.c_wchar_p(ico_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        WM_SETICON = 0x0080
        if big:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, big)
        if small:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, small)
    except Exception:  # noqa: BLE001
        pass


def run(port: int = 7892, title: str = TITLE_PREFIX) -> int:
    """开窗口并阻塞到用户关掉它。返回进程退出码。"""
    _set_app_id()   # 必须在创建任何窗口之前
    # 先自愈：带 MOTW 的程序集一定加载失败，做完再谈别的
    try:
        unblock_tree()
    except Exception:  # noqa: BLE001
        pass

    ok, why = available()
    if not ok:
        _fatal(f"原生窗口装不起来：{why}\n\n请重新安装本程序，或用浏览器打开 http://127.0.0.1:{port}/")
        return 2

    import webview

    # 后台没起来时开窗口只会得到一张连不上的白屏，不如直接说清楚
    if not backend_alive(port):
        _fatal(
            f"后台没有在运行（127.0.0.1:{port} 没有响应）。\n\n"
            "请先双击 VPNTray.exe 启动主程序，再打开控制台。",
        )
        return 6

    geo = _load_geo()
    try:
        from . import __version__ as _v
        title = f"{title} v{_v}"
    except Exception:  # noqa: BLE001
        pass

    try:
        win = webview.create_window(
            title,
            f"http://127.0.0.1:{port}/",
            width=geo["w"],
            height=geo["h"],
            min_size=(1000, 680),
            background_color="#0b0f14",
            text_select=True,
            easy_drag=False,
        )
    except Exception as e:  # noqa: BLE001
        _fatal(f"创建窗口失败：{type(e).__name__}: {e}")
        return 3

    def _remember() -> None:
        try:
            _save_geo(win.width, win.height)
        except Exception:  # noqa: BLE001
            pass

    try:
        win.events.closed += _remember
    except Exception:  # noqa: BLE001
        pass

    # 显式取图标路径，窗口显示后还要用它补一刀
    icon_path = ""
    try:
        cand = paths.asset("app.ico")
        if cand.exists():
            icon_path = str(cand)
    except Exception:  # noqa: BLE001
        icon_path = ""

    def _pin_icon() -> None:
        """Form.Icon 有时管不到任务栏按钮，窗口出来后再对 hwnd 钉一次。"""
        if not icon_path:
            return
        try:
            hwnd = int(win.native.Handle.ToInt64())
        except Exception:  # noqa: BLE001
            return
        # 窗口刚 show 时 papi 还没完全建好，稍等一下再钉
        for _ in range(6):
            _force_icon(hwnd, icon_path)
            time.sleep(0.25)

    try:
        win.events.shown += lambda: threading.Thread(target=_pin_icon, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass

    try:
        webview.start(gui="edgechromium", debug=bool(os.environ.get("VPNTRAY_UI_DEBUG")),
                      icon=icon_path or None)
    except Exception as e:  # noqa: BLE001
        # 这里是 2.0.0 静默失败的地方：异常被 DETACHED 进程的 stderr 吞掉，
        # 父进程只看 Popen 成功就以为打开了窗口。
        _fatal(
            "控制台窗口无法启动：\n\n"
            f"{type(e).__name__}: {e}\n\n"
            "常见原因：文件被 Windows 标记为「来自 Internet」，"
            "或缺少 WebView2 运行时。\n"
            f"可以先用浏览器打开 http://127.0.0.1:{port}/ 继续使用。",
        )
        return 4
    return 0


def main(argv: list[str]) -> int:
    port = 7892
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
    try:
        return run(port)
    except Exception as e:  # noqa: BLE001
        _fatal(f"窗口进程异常：{type(e).__name__}: {e}")
        return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
