r"""窗口控制台：原生窗口优先，起不来就退到 Edge/Chrome 的 app 模式窗口。

为什么要单独一个进程：主进程的主线程被 pywin32 的托盘消息泵占着，
pywebview 在 Windows 上又必须拿到主线程（WinForms 的 STA + 消息泵），
两者抢不了同一个线程。所以窗口跑在自己的进程里，靠本地 HTTP 和后台通信 ——
界面本身一行都不用改，后台也完全不知道窗口长什么样。

用法：
    VPNTray.exe --window --port 7892

三条路，一条比一条更少依赖环境
------------------------------
1. 原生窗口（pywebview + WebView2）。观感最好：自绘标题栏、自己的任务栏图标
   和 AppID。但它底下要 pythonnet 把 .NET Framework 的 Python.Runtime.dll 拉起来。
2. 浏览器 app 模式：msedge.exe --app=http://127.0.0.1:7892/ --window-size=W,H 。
   同样是无地址栏、无标签页的独立窗口，整条链路不碰 .NET / pythonnet。
3. 系统默认浏览器（一个普通标签页）。最后的兜底，绝不静默。

为什么必须有第 2 条：本机上 pythonnet 初始化会失败 ——
RuntimeError: Failed to resolve Python.Runtime.Loader.Initialize from
...\_internal\pythonnet\runtime\Python.Runtime.dll（同一个 dll 放到 site-packages
下就能加载，说明不是文件坏了，而是 .NET 的 Assembly.LoadFrom 在这个目录被挡了）。
CrashDumps 里还有 游戏加加（GPP64.dll）注入后把 clr.dll 撞成 0xc0000005 的记录。
这类环境因素我们改不动，能改的是「别把开窗口这件事押在 .NET 上」。

失败会记进 data/window-mode.json：下次点「打开控制台」直接走 app 模式窗口，
不再让用户干等原生窗口那 6 秒；换了新版本（__version__ 变了）会自动重试原生窗口。

MOTW 自愈
---------
从浏览器下载的 zip 解压后，每个文件会带一个 Zone.Identifier 备用数据流（俗称
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
# 记录「这台机器上原生窗口起不来」：下次直接走浏览器 app 模式
MODE_FILE = paths.DATA / "window-mode.json"
DEFAULT_GEO = {"w": 1400, "h": 920}
# 窗口从 create Popen 到真的出现，实测 1.2–2.5 秒（WebView2 首次加载更久）
SPAWN_WAIT = 6.0
# 浏览器 app 模式：进程可能把命令甩给已有实例就退出，只能等窗口；给足时间
BROWSER_WAIT = 6.5
# Edge/Chrome 的顶层窗口类名（app 模式窗口也是它）
BROWSER_CLASS = "Chrome_WidgetWin_1"
# 新窗口认出来之后，再花几秒看标题有没有变成我们的页面（只用于日志/诊断）
BROWSER_TITLE_WAIT = 3.0
# 一次性尝试浏览器窗口的总预算（多个候选 × 有无独立 profile）
BROWSER_BUDGET = 16.0
# 父进程自己会兜底（app 模式窗口 / 浏览器），子进程别再弹模态框挡路
QUIET_ENV = "VPNTRAY_UI_QUIET"


def _app_root() -> Path:
    """程序根目录：冻结后是 exe 所在目录，源码模式是仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _version() -> str:
    try:
        from . import __version__ as v

        return str(v)
    except Exception:  # noqa: BLE001
        return ""


def _quiet() -> bool:
    """父进程要求「别弹窗、只记日志」。"""
    return bool(os.environ.get(QUIET_ENV))


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
    直接删那个备用数据流：path:Zone.Identifier 在 Win32 层就是一个可删的文件名。
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
    """窗口起不来时必须让用户看见，不能静默。

    QUIET 模式下只记日志：这时候父进程已经在开 app 模式窗口了，
    再弹一个「控制台窗口无法启动」的模态框只会挡住用户。
    """
    log.error("%s", text.replace("\n", " | "))
    if _quiet():
        return
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


def _load_mode() -> dict:
    try:
        d = json.loads(MODE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _native_known_bad() -> bool:
    """上一轮原生窗口失败过、且还是同一个版本 → 直接走 app 模式窗口。

    为什么按版本失效：用户可能装了 WebView2 运行时、卸了游戏加加、
    或者在安全软件里放行了 dll —— 升级后就该重新试一次原生窗口。
    """
    d = _load_mode()
    return bool(d.get("native_bad")) and str(d.get("ver") or "") == _version()


def _remember_native_bad(why: str) -> None:
    try:
        paths.ensure_dirs()
        MODE_FILE.write_text(
            json.dumps(
                {"native_bad": True, "ver": _version(), "why": why,
                 "ts": int(time.time())},
                ensure_ascii=False, indent=2),
            encoding="utf-8")
        log.info("已记下「原生窗口在本机起不来」，下次直接用浏览器窗口（%s）", MODE_FILE)
    except Exception as e:  # noqa: BLE001
        log.debug("记录窗口模式失败: %s", e)


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
    程序集可能加载失败（MOTW / 缺 DLL / 被安全软件挡）。真正的判断只能靠
    spawn() 之后验活。
    """
    try:
        import webview  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"没装 pywebview（{type(e).__name__}: {e}）"
    return True, ""


def _find_console_window() -> int:
    """枚举顶层窗口，找标题以 TITLE_PREFIX 开头的那个。返回 hwnd 或 0。

    原生窗口的标题是「VPN Tray 控制台 v2.2.1」，Edge/Chrome app 模式的窗口
    标题就是网页 title「VPN Tray 控制台」—— 两种都归这一个前缀。
    """
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


def _browser_windows() -> dict[int, str]:
    """当前所有可见的 Edge/Chrome 顶层窗口 {hwnd: 标题}。"""
    out: dict[int, str] = {}
    try:
        u32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _cb(hwnd, _lparam):  # noqa: ANN001
            try:
                if not u32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                u32.GetClassNameW(hwnd, cls, 256)
                if cls.value != BROWSER_CLASS:
                    return True
                n = u32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                u32.GetWindowTextW(hwnd, buf, n + 1)
                out[hwnd] = buf.value
            except Exception:  # noqa: BLE001
                pass
            return True

        u32.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        return out
    return out


def _wait_browser_window(before: dict[int, str], wait: float) -> tuple[bool, str]:
    """等 app 模式窗口出来。返回 (成功?, 看到的标题)。

    **不能只认标题**：Edge 的 app 窗口刚创建时标题还是网址（"127.0.0.1_/"），
    要等文档加载完页面 title 才接管。2.2.1 第一次上机就栽在这里 —— 窗口其实
    已经开出来了，我们却说「没有出现窗口」，于是又开第二个、又判失败，最后
    退回浏览器标签页。所以改成：**出现新的浏览器窗口句柄就算成功**，标题只在
    后面几秒里顺便确认一下，用来写日志；标题没变成我们的页面也不判失败
    （慢机器上页面可能还在加载）。
    """
    deadline = time.time() + max(1.5, wait)
    while time.time() < deadline:
        if _find_console_window():
            return True, TITLE_PREFIX
        fresh = {h: t for h, t in _browser_windows().items() if h not in before}
        if fresh:
            hwnd, title = next(iter(fresh.items()))
            end = time.time() + BROWSER_TITLE_WAIT
            while time.time() < end:
                cur = _browser_windows()
                if hwnd not in cur:  # 闪一下就关：多半是被安全软件或用户点了关闭
                    break
                if cur[hwnd].startswith(TITLE_PREFIX):
                    return True, cur[hwnd]
                time.sleep(0.2)
            if hwnd in _browser_windows():
                return True, title
            return False, ""
        time.sleep(0.25)
    return False, ""


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

    env = os.environ.copy()
    # 子进程失败时只记日志：父进程马上会试 app 模式窗口，别先弹一个模态框
    env[QUIET_ENV] = "1"
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(root),
            close_fds=True,
            env=env,
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


def _browser_candidates() -> list[tuple[str, Path]]:
    """找出可用的 Edge / Chrome。返回 [(名字, exe 路径)]，按优先级排。"""
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()

    def add(kind: str, p) -> None:
        if not p:
            return
        try:
            pp = Path(str(p).strip().strip('"'))
        except (OSError, ValueError):
            return
        key = str(pp).lower()
        if key in seen or not pp.is_file():
            return
        seen.add(key)
        out.append((kind, pp))

    # ① 注册表 App Paths：装在非默认位置也找得到
    try:
        import winreg

        for exe, kind in (("msedge.exe", "Edge"), ("chrome.exe", "Chrome")):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                            hive,
                            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                        add(kind, winreg.QueryValueEx(k, None)[0])
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        pass

    # ② 默认安装路径
    bases = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
        r"C:\Program Files (x86)",
        r"C:\Program Files",
    ]
    for b in bases:
        if not b:
            continue
        add("Edge", Path(b) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        add("Chrome", Path(b) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return out


def _app_profile() -> Path:
    return paths.DATA / "appwindow-profile"


def spawn_browser(port: int = 7892, wait: float = BROWSER_WAIT) -> tuple[bool, str]:
    """用 Edge/Chrome 的 --app 模式开一个独立窗口（无地址栏、无标签页）。

    这条路完全不碰 .NET：原生窗口那套 pythonnet 初始化在本机被挡过，
    而「打开控制台」是高频操作，不能赌它今天正常。
    先试默认 profile（跟用户平时登录/扩展一致），不行再用独立 profile
    （独立 profile 一定能开窗口，代价是首次可能慢一点）。
    """
    geo = _load_geo()
    url = f"http://127.0.0.1:{port}/"
    cands = _browser_candidates()
    if not cands:
        return False, "没找到 Edge / Chrome"

    root = _app_root()
    last = ""
    deadline = time.time() + BROWSER_BUDGET
    for kind, exe in cands[:3]:
        for profile in ("", str(_app_profile())):
            left = deadline - time.time()
            if left < 1.5:
                return False, last or "浏览器 app 模式超时"
            cmd = [
                str(exe),
                f"--app={url}",
                f"--window-size={geo['w']},{geo['h']}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if profile:
                cmd.append(f"--user-data-dir={profile}")
            before = _browser_windows()   # 开窗前的窗口快照，用来认「新窗口」
            try:
                subprocess.Popen(cmd, cwd=str(root), close_fds=True,
                                 creationflags=0x00000008)
            except OSError as e:
                last = f"{kind} 起不来: {e}"
                log.warning("浏览器 app 模式启动失败：%s", last)
                break
            # 浏览器可能把命令甩给已有实例后自己立刻退出，不能凭退出码判断
            good, title = _wait_browser_window(before, min(wait, max(1.5, left)))
            if good:
                log.info("已用 %s 的 app 模式打开控制台（profile=%s，窗口标题=%s）",
                         kind, profile or "默认", title or "（页面还在加载）")
                return True, ""
            last = f"{kind} 没有出现窗口（profile={profile or '默认'}）"
            log.warning("浏览器 app 模式未出窗口：%s", last)
    return False, last or "浏览器 app 模式失败"


def open_console(port: int = 7892) -> str:
    """开控制台窗口。返回实际用的方式：focus / window / appwindow / browser / none。

    顺序：已有窗口 → 原生窗口 → Edge/Chrome app 模式窗口 → 默认浏览器。
    每条路都记日志，绝不再出现「点了没反应」。
    """
    if focus():
        return "focus"

    ok, why = available()
    if ok and not _native_known_bad():
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
        _remember_native_bad(err)
    elif ok:
        log.info("上次原生窗口在本机起不来，本轮直接走浏览器窗口")
    else:
        log.warning("pywebview 不可用：%s", why)

    good, err = spawn_browser(port)
    if good:
        return "appwindow"
    log.warning("浏览器窗口也起不来：%s", err)

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
            "常见原因：pythonnet/.NET 被安全软件挡住、文件被标记为「来自 Internet」，"
            "或缺少 WebView2 运行时。\n"
            f"关掉本提示后程序会自动改用 Edge/Chrome 窗口打开 "
            f"http://127.0.0.1:{port}/ 。",
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
