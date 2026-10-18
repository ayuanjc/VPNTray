"""原生 Win32 托盘（pywin32 实现）。

不用 pystray：它的动态菜单在 Windows 上经常弹不出来（菜单内容在 TrackPopupMenu
之前同步构建，节点多的时候会卡住/失效）。这里直接操作 HMENU，100+ 节点也是秒开。

菜单分两级：固定项 + 「切换节点 ▸ 地区 ▸ 节点」。双击图标打开 Web 控制台。
"""
from __future__ import annotations

import logging
import ctypes
import os
import threading
import time
import webbrowser
from typing import Callable

import win32api
import win32con
import win32gui


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NID(ctypes.Structure):
    """NOTIFYICONDATAW：pywin32 不导出这个结构体，气泡通知只能走 ctypes。"""

    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", _GUID),
        ("hBalloonIcon", ctypes.c_void_p),
    ]

from .paths import asset

log = logging.getLogger("tray")

WM_TRAY = win32con.WM_APP + 1
WM_REFRESH = win32con.WM_APP + 2
WM_TASK = win32con.WM_APP + 3

CLASS_NAME = "VPNTrayNativeWnd"
TITLE = "VPN Tray"

# 固定菜单项 ID
ID_STATUS = 1000
ID_OPEN = 1001
ID_MODE_RULE = 1002
ID_MODE_GLOBAL = 1003
ID_MODE_DIRECT = 1004
ID_BEST = 1005
ID_TEST = 1006
ID_SYSPROXY = 1007
ID_AUTOSTART = 1008
ID_AUTOBEST = 1009
ID_HEAL = 1010
ID_SUB = 1011
ID_RESTART = 1012
ID_CLOSE_CONNS = 1013
ID_QUIT = 1014
ID_TRAFFIC = 1015
ID_INFO = 1016

ID_NODE_BASE = 20000

ICONS = {
    "on": lambda: asset("app.ico"),
    "off": lambda: asset("app_off.ico"),
    "busy": lambda: asset("app_busy.ico"),
    "error": lambda: asset("app_err.ico"),
}

FLAG_ORDER = ["🇭🇰", "🇸🇬", "🇯🇵", "🇹🇼", "🇺🇸", "🇰🇷", "🇲🇾", "🇹🇭", "🇬🇧", "🇩🇪",
              "🇫🇷", "🇨🇦", "🇦🇺", "🇮🇳", "🇵🇭", "🇻🇳", "🇮🇩", "🇦🇪", "🇹🇷", "🇧🇷"]


class TrayWin:
    def __init__(self, app, webui, on_exit: Callable[[], None] | None = None) -> None:
        self.app = app
        self.webui = webui
        self.on_exit = on_exit
        self.hwnd: int | None = None
        self.hicon: int | None = None
        self._icon_state = ""
        self._node_cmds: dict[int, str] = {}
        self._last_key: tuple | None = None
        self._menu_open = False
        self._taskqueue: list[Callable[[], None]] = []
        self._lock = threading.RLock()

        app.on_change = self.refresh
        app.on_notify = self.notify

    # ══════════ 窗口 ══════════
    def _register(self) -> None:
        wc = win32gui.WNDCLASS()
        wc.lpszClassName = CLASS_NAME
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error as e:
            if e.winerror != 1410:  # class already exists
                raise

    def create(self) -> None:
        self._register()
        self._ui_thread = threading.current_thread()
        self._shutting = False
        self._nid_added = False
        self.hwnd = win32gui.CreateWindow(
            CLASS_NAME, TITLE, win32con.WS_OVERLAPPED,
            0, 0, 0, 0, 0, 0, win32api.GetModuleHandle(None), None,
        )
        self._update_icon(force=True)

    # ══════════ 托盘图标 ══════════
    def _load_icon(self, state: str) -> int:
        path = (ICONS.get(state) or ICONS["on"])()
        if not path.exists():
            log.warning("图标文件缺失，回落到运行中图标: %s", path)
            path = ICONS["on"]()
        try:
            return win32gui.LoadImage(
                0, str(path), win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("加载图标失败，使用系统默认图标: %s", e)
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _nid(self, flags: int, tip: str = "", icon: int | None = None) -> tuple:
        return (self.hwnd, 0, flags, WM_TRAY, self.hicon if icon is None else icon, tip)

    def _update_icon(self, force: bool = False) -> None:
        st = self.app.snapshot()
        if st.get("error"):
            state = "error"
        elif st.get("busy") or not st.get("kernel_ok"):
            state = "busy"
        else:
            state = "on"
        tip = self._tooltip(st)

        if not force and state == self._icon_state and tip == getattr(self, "_tip", ""):
            return

        new_icon = self._load_icon(state)
        old = self.hicon
        need_add = not getattr(self, "_nid_added", False)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        try:
            if need_add:
                # 开机自启时 explorer 可能还没准备好，ADD 会失败。
                # 那种情况下不能把 hicon 记下来，否则之后永远走 MODIFY，图标再也不出现。
                win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._nid(flags, tip, new_icon))
                self._nid_added = True
            else:
                win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._nid(flags, tip, new_icon))
            self.hicon = new_icon
            self._icon_state = state
            self._tip = tip
            if old:
                try:
                    win32gui.DestroyIcon(old)
                except Exception:  # noqa: BLE001
                    pass
        except win32gui.error as e:
            log.debug("更新托盘图标失败: %s", e)
            if need_add:
                try:
                    win32gui.DestroyIcon(new_icon)
                except Exception:  # noqa: BLE001
                    pass
            self._icon_state = ""      # 下一轮继续试
            self._tip = tip

    def _re_add_icon(self) -> None:
        """explorer.exe 重启后所有托盘图标都会消失，得重新注册。"""
        self._nid_added = False
        try:
            self._update_icon(force=True)
        except Exception as e:  # noqa: BLE001
            log.debug("重新注册托盘图标失败: %s", e)

    def _tooltip(self, st: dict) -> str:
        if st.get("error"):
            return f"VPN Tray · {str(st['error'])[:60]}"
        if not st.get("kernel_ok"):
            return "VPN Tray · 未连接"
        t = st.get("traffic") or {}
        return (f"VPN Tray · {st.get('group_now') or '?'}\n"
                f"↑ {_fmt(t.get('up_speed'))}/s  ↓ {_fmt(t.get('down_speed'))}/s")

    def refresh(self) -> None:
        """App 状态变化 → 让 UI 线程刷新图标。"""
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, WM_REFRESH, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def notify(self, msg: str, title: str = "VPN Tray") -> None:
        if not self.app.settings.get("notify", True) or not self.hwnd:
            return
        # pywin32 的 Shell_NotifyIcon 只吃 6 元组，气泡字段（szInfo/szInfoTitle/dwInfoFlags）
        # 必须由 ctypes 的 NOTIFYICONDATAW 结构体承载，不能直接拼 9 元组。
        try:
            nid = _NID()
            nid.cbSize = ctypes.sizeof(_NID)
            nid.hWnd = self.hwnd
            nid.uID = 0
            nid.uFlags = win32gui.NIF_ICON | win32gui.NIF_INFO
            nid.hIcon = self.hicon or win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
            nid.szTip = (self._tip if hasattr(self, "_tip") else TITLE)[:120]
            nid.szInfo = str(msg)[:250]
            nid.szInfoTitle = str(title)[:60]
            nid.dwInfoFlags = win32gui.NIIF_INFO
            if not ctypes.windll.shell32.Shell_NotifyIconW(
                win32gui.NIM_MODIFY, ctypes.byref(nid)
            ):
                log.debug("气泡提示被系统拒绝: %s", msg[:40])
        except Exception as e:  # noqa: BLE001
            log.debug("气泡提示失败: %s", e)

    # ══════════ 菜单 ══════════
    def _show_menu(self) -> None:
        log.info("弹出托盘菜单")
        app = self.app
        st = app.snapshot()
        s = app.settings
        self._node_cmds.clear()

        menu = win32gui.CreatePopupMenu()
        sub_menus: list[int] = []
        try:
            # 状态行
            win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_GRAYED, ID_STATUS,
                                self._status_text(st))
            win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_GRAYED, ID_TRAFFIC,
                                self._traffic_text(st))
            for i, info in enumerate((st.get("info_nodes") or [])[:2]):
                win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_GRAYED,
                                    ID_INFO + i, f"账户：{info}")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')

            # 切换节点
            node_menu = self._build_node_menu(st)
            if node_menu:
                sub_menus.append(node_menu)
                win32gui.AppendMenu(menu, win32con.MF_POPUP, node_menu, "切换节点")

            # 模式
            mode_menu = win32gui.CreatePopupMenu()
            sub_menus.append(mode_menu)
            for text, mid, key in (("规则分流（国内直连）", ID_MODE_RULE, "rule"),
                                   ("全局代理", ID_MODE_GLOBAL, "global"),
                                   ("全部直连", ID_MODE_DIRECT, "direct")):
                flags = win32con.MF_STRING | (win32con.MF_CHECKED if st.get("mode") == key else 0)
                win32gui.AppendMenu(mode_menu, flags, mid, text)
            win32gui.AppendMenu(menu, win32con.MF_POPUP, mode_menu,
                                f"模式：{self._mode_text(st.get('mode'))}")

            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_BEST,
                                self._delay_text(st))
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')

            # 开关
            def check(on: bool) -> int:
                return win32con.MF_STRING | (win32con.MF_CHECKED if on else 0)

            # 系统代理的勾选要反映注册表真实状态：别的软件（或上次异常退出）
            # 把代理改走后，配置里的 system_proxy 还是 True，勾着但实际没生效
            win32gui.AppendMenu(menu, check(bool(st.get("sysproxy", s.get("system_proxy")))),
                                ID_SYSPROXY, "系统代理（接管浏览器/软件）")
            win32gui.AppendMenu(menu, check(bool(s.get("autostart"))), ID_AUTOSTART,
                                "开机自动启动")
            win32gui.AppendMenu(menu, check(bool(s.get("auto_best"))), ID_AUTOBEST,
                                "自动优选（测速后切最快）")
            win32gui.AppendMenu(menu, check(bool(s.get("heal", True))), ID_HEAL,
                                "节点自愈（失效自动换）")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')

            # 运维
            win32gui.AppendMenu(menu, win32con.MF_STRING | win32con.MF_DEFAULT, ID_OPEN,
                                "打开控制台")
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_SUB, self._sub_text(st))
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_RESTART, "重启内核")
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_CLOSE_CONNS, "断开全部连接")
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, '')
            win32gui.AppendMenu(menu, win32con.MF_STRING, ID_QUIT, "退出")

            x, y = win32gui.GetCursorPos()
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception as e:  # noqa: BLE001
                log.debug("SetForegroundWindow 失败: %s", e)
            log.debug("TrackPopupMenu at %s,%s items=%s", x, y, len(self._node_cmds))
            cmd = win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_RETURNCMD | win32con.TPM_RIGHTBUTTON
                | win32con.TPM_LEFTALIGN | win32con.TPM_BOTTOMALIGN,
                x, y, 0, self.hwnd, None,
            )
            log.info("菜单返回 cmd=%s", cmd)
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        finally:
            for m in sub_menus:
                try:
                    win32gui.DestroyMenu(m)
                except Exception:  # noqa: BLE001
                    pass
            try:
                win32gui.DestroyMenu(menu)
            except Exception:  # noqa: BLE001
                pass

        if cmd:
            self._on_command(cmd)

    def _build_node_menu(self, st: dict) -> int | None:
        nodes = st.get("group_all") or []
        proxies = st.get("proxies") or {}
        if not nodes:
            return None
        group_types = {"selector", "urltest", "fallback", "loadbalance"}
        delays = st.get("delays") or {}
        cur = st.get("group_now")

        buckets: dict[str, list[str]] = {}
        special: list[str] = []
        # proxies 里查不到 type 的名字不是真节点 —— 那是「剩余流量：120 GB」这类
        # 信息项，混进切换菜单会让用户点了没反应。proxies 整块缺失时才放行全部。
        has_meta = bool(proxies)
        for n in nodes:
            ptype = str((proxies.get(n) or {}).get("type") or "").lower()
            if ptype in group_types:
                continue
            if has_meta and not ptype:
                continue
            flag = n[:2] if n and 0x1F1E6 <= ord(n[0]) <= 0x1F1FF else ""
            if flag:
                buckets.setdefault(flag, []).append(n)
            else:
                special.append(n)

        # 每次重建都从同一个起始 ID 重新编号，避免上一轮的残留命令
        self._node_cmds.clear()
        root = win32gui.CreatePopupMenu()
        subs: list[int] = []
        try:
            cmd = ID_NODE_BASE
            for n in special:
                win32gui.AppendMenu(root, self._item_flags(n == cur), cmd, self._node_label(n, delays, cur))
                self._node_cmds[cmd] = n
                cmd += 1
            if special:
                win32gui.AppendMenu(root, win32con.MF_SEPARATOR, 0, '')

            order = [f for f in FLAG_ORDER if f in buckets] + \
                    [f for f in buckets if f not in FLAG_ORDER]
            for flag in order:
                names = sorted(buckets[flag], key=_natural)
                sub = win32gui.CreatePopupMenu()
                subs.append(sub)          # 挂到 root 之前失败的话，得自己销毁
                for n in names:
                    win32gui.AppendMenu(sub, self._item_flags(n == cur), cmd, self._node_label(n, delays, cur))
                    self._node_cmds[cmd] = n
                    cmd += 1
                best = min((delays.get(n) for n in names if delays.get(n)), default=None)
                label = f"{flag} {names[0].split('|')[-1].split('-')[0][:6]} ({len(names)})"
                if best:
                    label += f" {best}ms"
                win32gui.AppendMenu(root, win32con.MF_POPUP, sub, label)
                subs.remove(sub)          # 所有权已交给 root，不能再单独销毁
            return root
        except Exception:
            # 任何一步抛错都要把已建出来的菜单还回去，否则右键菜单从此没反应
            for h in subs:
                try:
                    win32gui.DestroyMenu(h)
                except Exception:
                    pass
            try:
                win32gui.DestroyMenu(root)
            except Exception:
                pass
            self._node_cmds.clear()
            log.exception("构建节点菜单失败，本次只显示固定菜单项")
            return None

    @staticmethod
    def _item_flags(checked: bool) -> int:
        return win32con.MF_STRING | (win32con.MF_CHECKED if checked else 0)

    @staticmethod
    def _node_label(name: str, delays: dict, cur: str | None) -> str:
        d = delays.get(name)
        tag = f"  {d}ms" if d else "  -"
        # Win32 菜单把 & 当助记符前缀，节点名里带 & 会显示成下划线，得转义成 &&
        return f"{str(name).replace('&', '&&')}{tag}"

    # ══════════ 文案 ══════════
    @staticmethod
    def _status_text(st: dict) -> str:
        if st.get("error"):
            return f"⚠ {str(st['error'])[:60]}"
        if st.get("busy"):
            return f"… {st['busy']}"
        if not st.get("kernel_ok"):
            return "○ 未连接"
        px = "代理开" if st.get("sysproxy") else "代理关"
        return f"● {st.get('group_now') or '?'}   [{st.get('mode')} · {px}]"

    @staticmethod
    def _traffic_text(st: dict) -> str:
        t = st.get("traffic") or {}
        return (f"↑ {_fmt(t.get('up_speed'))}/s  ↓ {_fmt(t.get('down_speed'))}/s"
                f"   · {st.get('connections', 0)} 连接")

    @staticmethod
    def _mode_text(mode: str | None) -> str:
        return {"rule": "规则分流", "global": "全局代理", "direct": "全部直连"}.get(mode or "", mode or "")

    def _delay_text(self, st: dict) -> str:
        delays = st.get("delays") or {}
        if not delays:
            return "测速 / 自动优选"
        best = min(delays.items(), key=lambda kv: kv[1])
        return f"测速 / 自动优选（最快 {best[1]}ms）"

    def _sub_text(self, st: dict) -> str:
        if st.get("sub_error"):
            return f"更新订阅（失败：{str(st['sub_error'])[:24]}）"
        return f"更新订阅（{st.get('sub_nodes', 0)} 节点）"

    # ══════════ 命令 ══════════
    def _on_command(self, cmd: int) -> None:
        app = self.app
        if cmd in self._node_cmds:
            name = self._node_cmds[cmd]
            self._bg(app.select_node, name)
            return
        mapping = {
            ID_OPEN: self.open_console,
            ID_MODE_RULE: lambda: app.set_mode("rule"),
            ID_MODE_GLOBAL: lambda: app.set_mode("global"),
            ID_MODE_DIRECT: lambda: app.set_mode("direct"),
            ID_BEST: app.auto_pick_best,
            ID_SYSPROXY: lambda: app.toggle_system_proxy(not app.settings.get("system_proxy", True)),
            ID_AUTOSTART: lambda: app.set_autostart(not app.settings.get("autostart", False)),
            ID_AUTOBEST: self._toggle_auto_best,
            ID_HEAL: self._toggle_heal,
            ID_SUB: lambda: app.update_subscription(True),
            ID_RESTART: app.restart_kernel,
            ID_CLOSE_CONNS: app.close_connections,
            ID_QUIT: self._quit,
        }
        fn = mapping.get(cmd)
        if fn:
            self._bg(fn)

    def _toggle_auto_best(self) -> None:
        cur = bool(self.app.settings.get("auto_best", False))
        self.app.settings["auto_best"] = not cur
        self.app.settings.save()
        self.app.notify("已开启自动优选" if not cur else "已关闭自动优选")

    def _toggle_heal(self) -> None:
        cur = bool(self.app.settings.get("heal", True))
        self.app.settings["heal"] = not cur
        self.app.settings.save()
        self.app.notify("已开启节点自愈" if not cur else "已关闭节点自愈")

    def _bg(self, fn, *a) -> None:
        def run():
            try:
                fn(*a)
            except Exception as e:  # noqa: BLE001
                log.exception("菜单动作失败: %s", e)
                self.notify(f"操作失败：{e}")
        threading.Thread(target=run, daemon=True).start()

    def open_console(self) -> None:
        url = self.webui.url if self.webui else f"http://127.0.0.1:7892/"
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            log.warning("打开浏览器失败: %s", e)
        self.notify(f"控制台：{url}")

    def _quit(self) -> None:
        # 可能从 Web 面板线程调用，必须切回 UI 线程做清理
        hwnd = self.hwnd
        if threading.current_thread() is not getattr(self, "_ui_thread", None) and hwnd:
            try:
                win32gui.PostMessage(hwnd, WM_TASK, 0, 0)
                return
            except Exception:  # noqa: BLE001
                pass
        # 走到这里有两种情况：本来就在 UI 线程，或者窗口还没建好（启动早期就从面板点了退出）。
        # 后者如果直接 return，退出请求就丢了 —— 面板关了但进程还在。

        log.info("开始退出")
        if getattr(self, "_shutting", False):
            return                      # 连点两次「退出」不该起两套清理
        self._shutting = True
        self.notify("正在退出…", "VPN Tray")

        def cleanup() -> None:
            try:
                self.app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("退出时清理失败")
            try:
                if self.webui:
                    self.webui.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_DESTROY, 0, 0)
            except Exception:  # noqa: BLE001
                pass
            # 兜底：消息循环万一套不出来，也要保证进程真的结束
            time.sleep(0.6)
            log.info("进程强制结束")
            os._exit(0)

        threading.Thread(target=cleanup, name="quit", daemon=True).start()
        # 看门狗：清理线程卡在某个 join 上时也要让用户能退出
        def watchdog() -> None:
            time.sleep(12.0)
            log.warning("退出超时，强制结束进程")
            os._exit(0)
        threading.Thread(target=watchdog, name="quit-watchdog", daemon=True).start()

    # ══════════ 消息循环 ══════════
    def _hard_shutdown(self, why: str) -> None:
        """同步清理（关机/注销时必须走这里，没时间等线程）。"""
        if getattr(self, "_shutting", False):
            return
        self._shutting = True
        log.info("系统退出，开始清理: %s", why)
        try:
            self.app.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("退出清理失败")

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF if lparam > 0xFFFF else lparam
                log.debug("托盘消息 0x%04X", ev)
                if ev in (win32con.WM_RBUTTONUP, win32con.WM_CONTEXTMENU):
                    self._show_menu()
                elif ev == win32con.WM_LBUTTONDBLCLK:
                    self._bg(self.open_console)
                return True
            if msg == WM_REFRESH:
                self._update_icon()
                return True
            if msg == WM_TASK:
                self._quit()
                return True
            if msg == getattr(self, "_msg_taskbar_created", None):
                # explorer.exe 重启，托盘区被重建，图标需要重新注册
                self._re_add_icon()
                return True
            if msg == win32con.WM_QUERYENDSESSION:
                # 关机/注销前微软只给几秒：必须在这里同步还原系统代理，
                # 否则注册表会指着已经退出的 127.0.0.1:7891，下次开机全系统断网。
                self._hard_shutdown("WM_QUERYENDSESSION")
                return True
            if msg == win32con.WM_ENDSESSION:
                if wparam:
                    self._hard_shutdown("WM_ENDSESSION")
                return True
            if msg == win32con.WM_DESTROY:
                try:
                    win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (hwnd, 0))
                except Exception:  # noqa: BLE001
                    pass
                win32gui.PostQuitMessage(0)
                return True
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
        except Exception:  # noqa: BLE001
            log.exception("窗口消息处理失败 msg=0x%04X", msg)
            return True

    def run(self) -> None:
        self.create()
        try:
            self._msg_taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        except Exception:  # noqa: BLE001
            self._msg_taskbar_created = None
        log.info("原生托盘已创建，双击图标打开控制台")
        win32gui.PumpMessages()


def _fmt(n) -> str:
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def _natural(s: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
