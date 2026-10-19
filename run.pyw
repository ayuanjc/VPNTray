"""双击启动入口（无控制台窗口）。

结构：
    原生 Win32 托盘（右键菜单 / 双击开控制台）
      └── 本地 Web 控制台 http://127.0.0.1:7892/ （主界面）
            └── App（订阅 / 内核 / 分流 / 系统代理）

参数：
    --pystray    使用旧的 pystray 托盘（备用）
    --no-web     不启动 Web 控制台
    --console    同时打开浏览器控制台
    --window     只开原生窗口（独立进程，见 vpntray/ui_native.py）
    --port N     配合 --window 指定后台端口
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MUTEX_NAME = "Local\\VPNTray_SingleInstance_9f3a"
_MUTEX_HANDLE = None


def _already_running() -> bool:
    """命名互斥体保证单实例。

    用 Local\\ 而不是 Global\\：Global 命名对象要 SeCreateGlobalPrivilege，
    普通用户拿不到时 CreateMutexW 直接失败，判断就永远返回 False ——
    于是两个实例都起来了，第二个会把第一个的内核顶掉。
    """
    global _MUTEX_HANDLE
    try:
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, MUTEX_NAME)
        if not h:                     # 拿不到句柄就别拦，宁可多开也别打不开
            return False
        _MUTEX_HANDLE = h             # 留到进程退出，系统自动释放
        return k32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001
        return False


def _msgbox(text: str, flags: int = 0x40) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "VPN Tray", flags)
    except Exception:  # noqa: BLE001
        pass


def _spawn_console(port: int) -> bool:
    """为已经在跑的后台另起一个原生窗口进程。

    窗口不能和后台同进程：后台主线程被托盘的 Win32 消息泵占着，
    pywebview 又必须要主线程起 WinForms 消息泵，两者抢不了同一个线程。
    """
    try:
        if getattr(sys, "frozen", False):
            exe = sys.executable
            cmd = [exe, "--window", "--port", str(port)]
        else:
            exe = sys.executable or "pythonw.exe"
            # 源码模式下 sys.executable 可能是带控制台的 python.exe，换成 pythonw 免得弹黑框
            if exe.lower().endswith("python.exe"):
                alt = os.path.join(os.path.dirname(exe), "pythonw.exe")
                if os.path.isfile(alt):
                    exe = alt
            cmd = [exe, str(ROOT / "run.pyw"), "--window", "--port", str(port)]
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            close_fds=True,
            creationflags=0x00000008,  # DETACHED_PROCESS：窗口进程不跟着后台一起死
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _focus_console() -> bool:
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
            if buf.value.startswith("VPN Tray 控制台"):
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


def _open_existing() -> bool:
    """第二个实例启动时，直接把已经在跑的控制台打开。

    光弹一句「已经在运行了」等于让用户自己去找托盘图标，多数人找不到。
    端口是滚动的（7892 起占不到就 +1），所以逐个试，还要认一下页面确实是
    本程序（127.0.0.1 上的其它服务也会应答）。
    """
    import urllib.request

    if _focus_console():
        return True

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 别走系统代理
    for port in range(7892, 7900):
        url = f"http://127.0.0.1:{port}/"
        try:
            with opener.open(url, timeout=0.6) as r:
                head = r.read(2048).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        if "VPN Tray 控制台" not in head:
            continue
        if _spawn_console(port):
            return True
        # 原生窗口起不来（没装 pywebview / WebView2）才退回浏览器
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        return True
    return False


def main() -> int:
    # 原生窗口进程：只负责显示，不碰内核/订阅，所以不参与单实例检查
    if "--window" in sys.argv:
        from vpntray import ui_native  # noqa: PLC0415

        return ui_native.main(sys.argv[1:])

    if _already_running():
        if not _open_existing():
            _msgbox("VPN Tray 已经在运行了。\n\n双击右下角托盘图标（绿色 V）可以打开控制台。")
        return 0

    from vpntray import logger, paths  # noqa: PLC0415

    # 数据目录不可写（装在 Program Files、U 盘被拔、被安全软件锁住）时这里会抛异常，
    # windowed 模式没有控制台，用户只会看到「双击了但什么都没发生」。
    try:
        paths.ensure_dirs()
        logger.setup()
    except Exception as e:  # noqa: BLE001
        _msgbox(
            "VPN Tray 无法初始化：\n\n"
            f"{e}\n\n"
            "请把程序放到有写入权限的目录（例如桌面或 D 盘）再试。",
            0x10,
        )
        return 1
    log = logger.get("main")

    from vpntray.app import App  # noqa: PLC0415
    from vpntray.webui import WebUI  # noqa: PLC0415

    use_pystray = "--pystray" in sys.argv
    no_web = "--no-web" in sys.argv

    app = App()

    if use_pystray:
        from vpntray.tray import Tray  # noqa: PLC0415
        tray = Tray(app)
    else:
        from vpntray.tray_win import TrayWin  # noqa: PLC0415
        tray = TrayWin(app, None)

    webui = None
    if not no_web:
        webui = WebUI(app, on_quit=tray._quit)  # noqa: SLF001
        if hasattr(tray, "webui"):
            tray.webui = webui
        if not webui.start():
            log.warning("Web 控制台启动失败，仅使用托盘")

    def bootstrap() -> None:
        try:
            ok = app.start()
            if ok:
                app.notify(f"已连接 · {app.state.get('group_now') or '节点已就绪'}")
                if webui and "--console" in sys.argv:
                    import webbrowser
                    webbrowser.open(webui.url)
            else:
                app.notify(app.state.get("error") or "启动失败，请查看日志")
        except Exception:  # noqa: BLE001
            log.exception("启动异常")
            app._set(error="启动异常，详见 data/logs/app.log")  # noqa: SLF001

    threading.Thread(target=bootstrap, name="bootstrap", daemon=True).start()

    try:
        tray.run()
    except Exception:
        log.exception("托盘异常退出")
        _msgbox("VPN Tray 异常退出：\n\n" + traceback.format_exc()[-800:], 0x10)
        return 1
    finally:
        # 顺序很重要：先还原系统代理 + 停内核，再停面板。
        # 少了 app.shutdown() 就会出现「托盘退出后 mihomo 变孤儿、系统代理还指着它」→ 断网。
        try:
            app.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("退出清理失败")
        try:
            if webui:
                webui.stop()
        except Exception:  # noqa: BLE001
            pass

    log.info("进程退出")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:  # noqa: BLE001
        try:
            import logging
            logging.getLogger("main").exception("未捕获异常")
        except Exception:  # noqa: BLE001
            pass
        code = 1
    # frozen + windowed 下 sys.stdout 是 None，不能用 sys.stdout.flush()
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)
