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
import sys

# winmgmt（WMI 服务）卡死时，setuptools 的 distutils hack 会在导入期去查
# platform.system() → WMI，进程从此永久阻塞：没窗口、没日志、没端口，看起来就是
# 「软件点了没反应」（2026-11-08 实测，4 个僵尸进程全卡在 _wmi_query）。
# frozen 版的运行时钩子比这行更早跑，真正生效的是打包时排除
# setuptools / pkg_resources / _distutils_hack（见 release.ps1）；
# 这行是源码运行和开发机上的兜底。
os.environ.setdefault("SETUPTOOLS_USE_DISTUTILS", "stdlib")
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MUTEX_NAME = "Local\\VPNTray_SingleInstance_9f3a"
_MUTEX_HANDLE = None

# 启动路标：logger 起来之前，每走一步往 data/logs/boot.log 记一行。
# 2.5.1 之前这段路是黑的 —— 进程卡死时既没有窗口也没有日志，只能靠 py-spy
# 附加才看出它卡在 MessageBoxW 上（2026-11-07 那次「梯子炸了」就是这么查的）。
BOOT_MAX = 256 * 1024


def _boot_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data" / "logs"
    return ROOT / "data" / "logs"


def _boot(step: str) -> None:
    try:
        d = _boot_dir()
        d.mkdir(parents=True, exist_ok=True)
        p = d / "boot.log"
        if p.exists() and p.stat().st_size > BOOT_MAX:
            p.unlink()
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {step} pid={os.getpid()}\n")
    except Exception:  # noqa: BLE001
        pass


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


def _msgbox(text: str, flags: int = 0x40, seconds: float = 12.0) -> None:
    """非阻塞提示框（2.5.2 起）。

    以前是同步 MessageBoxW：没人点掉它，这个进程就永远卡在那一行 ——
    既不退出也不启动，外面看是「双击没反应」，于是用户再点一次就再堆一个。
    2026-11-07 现场堆了 8 个这样的僵尸进程，没有一个在监听端口。
    现在放后台线程里弹，最多等 seconds 秒，进程该退出就退出。
    """
    def _show() -> None:
        try:
            ctypes.windll.user32.MessageBoxW(None, text, "VPN Tray", flags)
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=_show, name="msgbox", daemon=True)
    t.start()
    t.join(seconds)


def _port_open(port: int, timeout: float = 0.35) -> bool:
    import socket

    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _probe_console() -> int:
    """在跑的实例用的是哪个端口（7892 起滚动）。

    认的是 /api/state 里的 ver 字段。2.5.1 之前这里读首页前 2KB 找
    "VPN Tray 控制台"，而页面标题其实是 VPNTray —— 永远匹配不上，
    于是「再点一次 exe 想打开控制台」每次都会掉进弹框分支。
    """
    import json
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 别走系统代理
    for port in range(7892, 7900):
        if not _port_open(port):
            continue
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=0.8) as r:
                d = json.loads(r.read(65536).decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            continue
        st = d.get("state") if isinstance(d, dict) else None
        if isinstance(st, dict) and st.get("ver"):
            return port
    return 0


def _open_existing(port: int = 0) -> bool:
    """第二个实例启动时，直接把已经在跑的控制台打开。

    光弹一句「已经在运行了」等于让用户自己去找托盘图标，多数人找不到。
    端口是滚动的（7892 起占不到就 +1），所以逐个试，还要认一下应答确实是
    本程序（127.0.0.1 上的其它服务也会应答）。

    开窗口的策略不在这里重写一遍：统一交给 ui_native.open_console，
    它管「原生窗口 → Edge/Chrome app 模式窗口 → 浏览器」这条链。
    以前这里自己只 spawn 原生窗口，起不来就丢给浏览器标签页 ——
    于是「托盘双击」和「再点一次 exe」打开的东西不一样。
    """
    from vpntray import logger as _lg  # noqa: PLC0415
    from vpntray import ui_native  # noqa: PLC0415

    # 第二个实例也要写日志，否则「打开控制台失败」只发生在别人看不见的地方
    try:
        _lg.setup()
    except Exception:  # noqa: BLE001
        pass

    deadline = time.monotonic() + 8.0
    _boot(f"open_existing:port={port}")

    # focus() 内部要 AttachThreadInput / SetForegroundWindow：对方进程卡死的时候
    # 这一行会跟着挂住，所以放进线程里并设预算，超时就走下一招，绝不死等。
    box: dict = {}

    def _focus() -> None:
        try:
            box["ok"] = bool(ui_native.focus())
        except Exception as e:  # noqa: BLE001
            box["err"] = repr(e)

    t = threading.Thread(target=_focus, name="focus", daemon=True)
    t.start()
    t.join(5.0)
    if box.get("ok"):
        return True
    _boot("open_existing:focus 没成功")

    port = port or _probe_console()
    if not port:
        return False
    _boot(f"open_existing:开窗口 on {port}")
    res: dict = {}

    def _open() -> None:
        try:
            res["r"] = ui_native.open_console(port)
        except Exception as e:  # noqa: BLE001
            res["err"] = repr(e)

    t2 = threading.Thread(target=_open, name="console", daemon=True)
    t2.start()
    t2.join(max(1.0, deadline - time.monotonic()))
    return res.get("r") not in (None, "none")


def _second_instance() -> bool:
    """判断「该不该由本进程接手」：True = 另一个活实例在，本进程退出。

    判据是**有没有活的服务**，不是「互斥体在不在」。2026-11-07 16:37 那次，
    上个实例猝死后互斥体留在系统里，之后 8 次双击全卡在「已经在运行了」的
    模态框上：面板没起、内核没起、系统代理还指着 7891 —— 对外就是全网断。
    现在只有面板（或至少内核端口）真的在，才认为另一个实例活着。
    """
    if not _already_running():
        return False
    port = _probe_console()
    if not port:
        for _ in range(12):          # 正常实例 2 秒内起面板，6 秒是给慢机器/热点的余量
            time.sleep(0.5)
            port = _probe_console()
            if port:
                break
    _boot(f"mutex 已被占用 console={port} kernel={_port_open(7891)}")
    if port:
        if not _open_existing(port):
            _msgbox("VPN Tray 已经在运行了。\n\n双击右下角托盘图标（绿色 V）可以打开控制台。")
        return True
    if _port_open(7891):
        _msgbox("VPN Tray 已经在运行，但控制台还没起来。\n\n等几秒再点，或到任务管理器结束 VPNTray.exe 后重开。")
        return True
    _boot("接管：互斥体是空壳（面板与内核都没应答）")
    try:
        from vpntray import logger as _lg2  # noqa: PLC0415
        from vpntray import paths as _p2  # noqa: PLC0415

        _p2.ensure_dirs()
        _lg2.setup()
        _lg2.get("main").warning(
            "单实例互斥体还在，但面板和内核都没应答（上次异常退出留下的空壳），本次直接接管启动"
        )
    except Exception:  # noqa: BLE001
        pass
    return False


def main() -> int:
    _boot("start " + " ".join(sys.argv[1:])[:120])
    # 原生窗口进程：只负责显示，不碰内核/订阅，所以不参与单实例检查
    if "--window" in sys.argv:
        _boot("--window 子进程")
        from vpntray import logger as _lg  # noqa: PLC0415
        from vpntray import paths as _p  # noqa: PLC0415
        from vpntray import ui_native  # noqa: PLC0415

        # 窗口进程也必须写日志：2.0.0 这里直接 return，子进程崩了连一行记录都没有，
        # 用户反馈「点了没反应」时无从查起。
        try:
            _p.ensure_dirs()
            _lg.setup()
        except Exception:  # noqa: BLE001
            pass
        return ui_native.main(sys.argv[1:])

    if _second_instance():
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
    _boot("paths+logger ok")

    # 从浏览器下载的 zip 解压后每个文件都带「来自 Internet」标记（Zone.Identifier）。
    # .NET 拒绝加载这种程序集，原生窗口就会起不来。这里先清一遍，用户不用知道这回事。
    try:
        from vpntray import ui_native as _un  # noqa: PLC0415

        _n = _un.unblock_tree()
        if _n:
            log.info("已清除 %d 个文件的 Internet 来源标记", _n)
    except Exception:  # noqa: BLE001
        log.warning("清理 Internet 来源标记失败（不影响主功能）", exc_info=True)
    _boot("unblock 完成")

    from vpntray.app import App  # noqa: PLC0415
    from vpntray.webui import WebUI  # noqa: PLC0415

    use_pystray = "--pystray" in sys.argv
    no_web = "--no-web" in sys.argv

    app = App()
    _boot("App 就绪")

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
        _boot(f"WebUI {'ok' if webui else 'fail'}")

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

    _boot("托盘就绪，进入消息循环")
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
