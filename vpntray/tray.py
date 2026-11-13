"""托盘 UI：动态菜单、节点分组、状态图标。"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from functools import partial

# 托盘是可选组件：pystray 在 Linux 上要 GTK/AppIndicator，缺库时导入就抛。
# 这里只降级不中断，让 webui / 命令行入口在没托盘的环境里照样能起来。
IMPORT_ERROR = ""
try:
    import pystray
    from pystray import Menu, MenuItem
except Exception as _e:  # noqa: BLE001
    pystray = None                    # type: ignore[assignment]
    Menu = None                       # type: ignore[assignment]
    MenuItem = None                   # type: ignore[assignment]
    IMPORT_ERROR = f"托盘不可用（pystray 导入失败）：{_e}"

from . import icons
from .app import App
from .subscription import is_info_node

log = logging.getLogger("tray")

FLAG_NAMES = {
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

INFO_MARKERS = ("剩余流量", "套餐到期", "官网", "群组", "订阅", "到期时间", "流量重置")


def _fmt_bytes(n: float) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_age(ts: float) -> str:
    if not ts:
        return "从未"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d} 秒前"
    if d < 3600:
        return f"{d} 分钟前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"


class Tray:
    def __init__(self, app: App) -> None:
        if pystray is None:
            raise RuntimeError(IMPORT_ERROR or "托盘不可用（pystray 不可用）")
        self.app = app
        self._last_state_key: tuple | None = None
        # 同一个动作不许并发跑两遍：托盘菜单会被连点，两个线程同时 set_mode /
        # 重启内核会互相打脸，也会把订阅接口打出两份并发请求
        self._bg_lock = threading.Lock()
        self._bg_busy: set[str] = set()
        self.icon = pystray.Icon(
            "vpntray",
            icons.make_icon("busy", 64),
            "VPN Tray · 启动中",
            self._build_menu(),
        )
        app.on_change = self._on_change
        app.on_notify = self._notify

    # ══════════ 菜单 ══════════
    def _build_menu(self) -> Menu:
        return Menu(self._menu_items)

    def _menu_items(self):
        st = self.app.snapshot()
        s = self.app.settings

        # —— 状态行 ——
        yield MenuItem(lambda i: self._status_text(st), self._noop, enabled=False, default=True)
        yield MenuItem(lambda i: self._traffic_text(st), self._noop, enabled=False)
        for info in (st.get("info_nodes") or [])[:2]:
            yield MenuItem(lambda i, t=info: f"账户：{t}", self._noop, enabled=False)
        yield Menu.SEPARATOR

        # —— 节点 ——
        yield MenuItem("切换节点", Menu(self._node_items))

        # —— 模式 ——
        yield MenuItem(
            lambda i: f"模式：{self._mode_text(st.get('mode') or 'rule')}",
            Menu(
                MenuItem("规则分流（国内直连）", lambda i, it: self._bg(self.app.set_mode, "rule"),
                         checked=lambda it: (st.get("mode") or "rule") == "rule", radio=True),
                MenuItem("全局代理", lambda i, it: self._bg(self.app.set_mode, "global"),
                         checked=lambda it: (st.get("mode") or "rule") == "global", radio=True),
                MenuItem("全部直连", lambda i, it: self._bg(self.app.set_mode, "direct"),
                         checked=lambda it: (st.get("mode") or "rule") == "direct", radio=True),
            ),
        )

        yield MenuItem(lambda i: self._delay_text(st), lambda i, it: self._bg(self.app.test_delays))
        yield Menu.SEPARATOR

        # —— 开关 ——
        yield MenuItem(
            "系统代理（接管浏览器/软件）",
            lambda i, it: self._bg(self.app.toggle_system_proxy, not self.app.settings.get("system_proxy", True)),
            checked=lambda it: bool(self.app.settings.get("system_proxy", True)),
        )
        yield MenuItem(
            "开机自动启动",
            lambda i, it: self._bg(self.app.set_autostart, not self.app.settings.get("autostart", False)),
            checked=lambda it: bool(self.app.settings.get("autostart", False)),
        )
        yield MenuItem(
            "自动优选（测速后切最快）",
            lambda i, it: self._toggle_auto_best(),
            checked=lambda it: bool(self.app.settings.get("auto_best", False)),
        )
        yield Menu.SEPARATOR

        # —— 运维 ——
        yield MenuItem(lambda i: self._sub_text(st), lambda i, it: self._bg(self.app.update_subscription, True))
        yield MenuItem("打开控制面板", lambda i, it: self._bg(self.app.open_dashboard))
        yield MenuItem("断开全部连接", lambda i, it: self._bg(self.app.close_connections))
        yield MenuItem("重启内核", lambda i, it: self._bg(self._restart_kernel))
        yield MenuItem("打开日志目录", lambda i, it: self._bg(self.app.open_log))
        yield Menu.SEPARATOR
        yield MenuItem("退出", lambda i, it: self._quit())

    # —— 节点子菜单 ——
    def _node_items(self):
        st = self.app.snapshot()
        all_nodes = st["group_all"]
        now = st["group_now"]
        delays = st["delays"]
        proxies = st["proxies"] or {}
        group_names = set(proxies.keys())

        special: list[str] = []
        by_flag: "OrderedDict[str, list[str]]" = OrderedDict()
        others: list[str] = []

        for n in all_nodes:
            if is_info_node(n):
                continue
            if n in ("DIRECT", "REJECT", "PASS") or n in group_names:
                special.append(n)
                continue
            flag = n[:2]
            if flag in FLAG_NAMES:
                by_flag.setdefault(FLAG_NAMES[flag], []).append(n)
            elif flag and 0x1F1E6 <= ord(flag[0]) <= 0x1F1FF:
                by_flag.setdefault(flag, []).append(n)
            else:
                others.append(n)

        if not all_nodes:
            yield MenuItem("（节点未加载）", self._noop, enabled=False)
            return

        for n in special:
            yield self._node_item(n, now, delays.get(n), bold_group=True)
        if special and (by_flag or others):
            yield Menu.SEPARATOR

        for region, names in by_flag.items():
            names_sorted = sorted(names, key=_natural_key)
            fastest = min((delays.get(x) for x in names_sorted if delays.get(x)), default=None)
            title = f"{region} · {len(names_sorted)}" + (f" · {fastest}ms" if fastest else "")
            yield MenuItem(
                title,
                Menu(*[self._node_item(x, now, delays.get(x)) for x in names_sorted]),
            )
        if others:
            yield MenuItem(
                "其它",
                Menu(*[self._node_item(x, now, delays.get(x)) for x in others]),
            )

    def _node_item(self, name: str, now: str, delay: int | None, bold_group: bool = False) -> MenuItem:
        marker = "●" if name == now else "○"
        d = f"  {delay}ms" if delay else ("  超时" if delay == 0 else "")
        label = f"{marker} {name}{d}"
        # 注意：pystray 要求 action 恰好 (icon, item) 两个参数、checked 恰好 (item) 一个，
        # 多一个默认参数都会在构建菜单时抛 ValueError。用 partial 绑定节点名。
        return MenuItem(
            label,
            partial(self._node_action, name),
            checked=partial(self._node_checked, name),
            radio=True,
        )

    def _node_action(self, name: str, icon, item) -> None:
        self._bg(self.app.select_node, name)

    def _node_checked(self, name: str, item) -> bool:
        return name == self.app.state.get("group_now")

    # ══════════ 文案 ══════════
    def _status_text(self, st: dict) -> str:
        if st.get("error"):
            return f"⚠ {st['error'][:60]}"
        if st.get("busy"):
            return f"… {st['busy']}"
        if not st.get("kernel_ok"):
            return "○ 未连接"
        now = st.get("group_now") or "?"
        proxy_on = "系统代理 开" if st.get("sysproxy") else "系统代理 关"
        return f"● {now}  [{self._mode_text(st.get('mode') or 'rule')} · {proxy_on}]"

    def _traffic_text(self, st: dict) -> str:
        t = st.get("traffic") or {}
        return (
            f"↑ {_fmt_bytes(t.get('up_speed', 0))}/s  "
            f"↓ {_fmt_bytes(t.get('down_speed', 0))}/s  "
            f"累计 ↓ {_fmt_bytes(t.get('down', 0))}  ·  {st.get('connections', 0)} 连接"
        )

    def _delay_text(self, st: dict) -> str:
        delays = st.get("delays") or {}
        if not delays:
            return "延迟测试（未测速）"
        best = min(delays.items(), key=lambda kv: kv[1])
        return f"延迟测试（{len(delays)} 节点，最快 {best[1]}ms）"

    def _sub_text(self, st: dict) -> str:
        if st.get("sub_error"):
            return f"更新订阅（失败：{st['sub_error'][:30]}）"
        return f"更新订阅（{st.get('sub_nodes', 0)} 节点 · {_fmt_age(st.get('sub_last_ok', 0))}）"

    @staticmethod
    def _mode_text(mode: str) -> str:
        return {"rule": "规则分流", "global": "全局代理", "direct": "全部直连"}.get(mode, mode)

    # ══════════ 动作 ══════════
    def _noop(self, *a) -> None:
        return None

    def _bg(self, fn, *args) -> None:
        """后台执行托盘动作，同一个动作在跑时忽略重复点击。

        按函数身份（__name__）判重，不同动作之间不互相阻塞：更新订阅很慢，
        但不能因此让「重启内核」也点不动。
        """
        key = getattr(fn, "__name__", repr(fn))
        with self._bg_lock:
            if key in self._bg_busy:
                log.debug("动作 %s 仍在执行，忽略这次点击", key)
                return
            self._bg_busy.add(key)

        def runner() -> None:
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                log.exception("托盘动作 %s 失败", key)
            finally:
                with self._bg_lock:
                    self._bg_busy.discard(key)

        threading.Thread(target=runner, daemon=True, name=f"tray-{key}").start()

    def _restart_kernel(self) -> None:
        """重启内核，并把「没成」的两种原因分别说出来。

        为什么不能直接把 app.restart_kernel 挂到菜单上：它现在返回 bool，菜单回调的
        返回值没人看，重建失败就成了「点了没反应」。另外它抢不到总锁时也返回 False，
        语义是「跳过」而不是失败（此时 state['error'] 为空），所以两种提示必须分开，
        否则用户会在正常并发时看到一条假的失败提示。
        """
        try:
            ok = self.app.restart_kernel()
        except Exception as e:  # noqa: BLE001
            log.exception("重启内核调用失败")
            self.app.notify(f"内核重启失败：{e}")
            return
        if ok:
            return  # 成功提示由 App 自己发，这里不重复弹
        err = str(self.app.state.get("error") or getattr(self.app.kernel, "last_error", "") or "")
        if err:
            self.app.notify(f"内核重启失败：{err[:120]}")
        else:
            self.app.notify("内核重启被跳过：有其它任务正在操作内核，请稍后重试")


    def _toggle_auto_best(self) -> None:
        cur = bool(self.app.settings.get("auto_best", False))
        self.app.settings["auto_best"] = not cur
        self.app.settings.save()
        self.app.notify("已开启自动优选" if not cur else "已关闭自动优选")

    def _quit(self) -> None:
        try:
            self.app.shutdown()
        finally:
            self.icon.stop()

    # ══════════ 刷新 ══════════
    def _on_change(self) -> None:
        st = self.app.snapshot()
        key = (
            st.get("kernel_ok"), st.get("busy"), st.get("error", "")[:40],
            st.get("group_now"), st.get("mode"), st.get("sysproxy"),
            len(st.get("group_all") or []), st.get("connections"),
        )
        icon_state = "error" if st.get("error") else (
            "busy" if (st.get("busy") or not st.get("kernel_ok")) else "on"
        )
        try:
            self.icon.icon = icons.make_icon(icon_state, 64)
            self.icon.title = self._tooltip(st)
        except Exception:  # noqa: BLE001
            # 换图标失败（explorer 重启、会话切换时 pystray 会抛）就别推进 key：
            # 推进了等于「这次算更新过」，下次状态相同就不会再试，托盘会一直
            # 停在旧图标上
            log.debug("更新托盘图标失败", exc_info=True)
            return
        if key != self._last_state_key:
            self._last_state_key = key
            try:
                self.icon.update_menu()
            except Exception:  # noqa: BLE001
                log.debug("刷新托盘菜单失败", exc_info=True)

    def _tooltip(self, st: dict) -> str:
        if st.get("error"):
            return f"VPN Tray · {st['error'][:80]}"
        if not st.get("kernel_ok"):
            return "VPN Tray · 未连接"
        t = st.get("traffic") or {}
        return (
            f"VPN Tray · {st.get('group_now') or '?'}\n"
            f"↑ {_fmt_bytes(t.get('up_speed', 0))}/s ↓ {_fmt_bytes(t.get('down_speed', 0))}/s\n"
            f"累计 ↓ {_fmt_bytes(t.get('down', 0))}"
        )

    def _notify(self, msg: str, title: str = "VPN Tray") -> None:
        try:
            self.icon.notify(msg, title)
        except Exception:  # noqa: BLE001
            pass

    # ══════════ 运行 ══════════
    def run(self) -> None:
        self.icon.run(setup=self._setup)

    def _setup(self, icon: pystray.Icon) -> None:
        icon.visible = True
        threading.Thread(target=self._bootstrap, name="bootstrap", daemon=True).start()

    def _bootstrap(self) -> None:
        try:
            ok = self.app.start()
        except Exception as e:  # noqa: BLE001
            log.exception("启动异常")
            self.app._set(error=f"启动异常：{e}")  # noqa: SLF001
            ok = False
        self._on_change()
        if ok:
            self.app.notify(f"已连接 · {self.app.state.get('group_now') or '节点已就绪'}")
        else:
            self.app.notify(self.app.state.get("error") or "启动失败，请查看日志")


def _natural_key(s: str):
    import re

    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
