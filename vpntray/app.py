"""应用核心：状态机 + 订阅/内核/测速/系统代理的编排。"""
from __future__ import annotations

import logging
import os
import threading
import time
import webbrowser
from typing import Any, Callable

import requests

from . import __version__ as _VERSION
from . import autostart, configgen, disguise, kernel as kernel_mod, netguard, purity, subscription, sysproxy
from .api import ApiError, MihomoAPI
from .kernel import Kernel
from .paths import CONFIG, LOGS, SUB_CACHE, UI_DIR, ensure_dirs
from .settings import Settings


def _num(v: Any, default: float = 0.0) -> float:
    """把内核回来的数字转成 float。

    bool 不算数字：Python 里 True + 1 == 2，直接用 float() 会把 true 当成 1 算进流量。
    NaN / ±inf 也一并挡掉，否则前端 toFixed 会抛错、排序会退化成随机。
    """
    if isinstance(v, bool):
        return default
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    if n != n or n in (float("inf"), float("-inf")):
        return default
    return n

log = logging.getLogger("app")


def _version() -> str:
    """当前版本号，和 webui.UI_VER 同源（都来自 vpntray/__init__）。"""
    return _VERSION


def _int(v: Any, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    """把 settings/JSON 里的脏值收敛成合法整数，越界回落默认值。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        return default
    if hi is not None and n > hi:
        return default
    return n


class App:
    def __init__(self) -> None:
        ensure_dirs()
        self.settings = Settings()
        self.api = MihomoAPI(
            port=int(self.settings["controller_port"]),
            secret=self.settings.get("secret", ""),
        )
        self.kernel = Kernel(self.api, self.settings)

        self.on_change: Callable[[], None] | None = None
        self.on_notify: Callable[[str, str], None] | None = None
        self.on_quit: Callable[[], None] | None = None   # 更新装完后要主动退出

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self.state: dict[str, Any] = {
            "phase": "空闲",
            "kernel_ok": False,
            "error": "",
            "mode": self.settings.get("mode", "rule"),
            "main_group": self.settings.get("main_group", "节点选择"),
            "group_now": "",
            "group_all": [],
            "delays": {},
            "proxies": {},
            "traffic": {"up": 0, "down": 0, "up_speed": 0, "down_speed": 0},
            "connections": 0,
            "sysproxy": False,
            "autostart": autostart.enabled(),
            "sub_last_ok": 0.0,
            "sub_last_try": 0.0,
            "sub_error": "",
            "sub_nodes": 0,
            "info_nodes": [],
            "purity": purity.load_all(),   # {节点名: 纯净度结果}
            "speed": {},                    # {节点名: 带宽结果}
            "progress": {"kind": "", "done": 0, "total": 0, "text": ""},
            "busy": "",
            "started_at": 0.0,
            "update": {},                   # 最近一次检查更新的结果
        }
        self._last_conn = (0, 0, 0.0)  # up, down, ts
        self._delays_ts = 0.0
        self._loops_started = False
        self._health_fail = 0
        self._purity_running = False
        self._speed_running = False
        self._probe_busy = False
        self._update_busy = False
        self._task_lock = threading.Lock()   # 检测类长任务的互斥

    def _claim_task(self, flag: str) -> bool:
        """「有没有任务在跑」+「置位」必须原子：面板双击就能并发两份，
        两份会互相抢节点，最终把出口 IP / 速度写到错节点上。"""
        with self._task_lock:
            if self._purity_running or self._speed_running:
                return False
            setattr(self, flag, True)
            return True

    # ══════════ 对外只读快照 ══════════
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = dict(self.state)
            s["group_all"] = list(self.state["group_all"])
            s["delays"] = dict(self.state["delays"])
            s["traffic"] = dict(self.state["traffic"])
            return s

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self.state.update(kw)
        self._changed()

    def _set_progress(self, kind: str = "", done: int = 0, total: int = 0, text: str = "") -> None:
        """长任务进度（前端进度条用）。不触发 UI 回调，靠轮询带出去。"""
        with self._lock:
            self.state["progress"] = {"kind": kind, "done": done, "total": total, "text": text}

    def _changed(self) -> None:
        cb = self.on_change
        if cb:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                log.debug("UI 回调异常: %s", e)

    def notify(self, msg: str, title: str = "VPN Tray") -> None:
        if not self.settings.get("notify", True):
            return
        cb = self.on_notify
        if cb:
            try:
                cb(msg, title)
            except Exception:  # noqa: BLE001
                pass

    # ══════════ 启动 / 关闭 ══════════
    def start(self) -> bool:
        self._set(phase="准备配置", busy="启动中")
        ok = self.prepare_config(initial=True)
        if not ok:
            # 「还没填订阅」是首次使用的正常起点，不是故障：
            # 报成「配置失败」会让新用户以为装坏了，界面也只能看到一条红色报错。
            err = str(self.state.get("error") or "")
            if not err or "未配置订阅" in err or "订阅缓存不可用" in err:
                self._set(phase="待添加订阅", error="", busy="")
                log.info("尚未配置订阅，等待用户添加")
            else:
                self._set(phase="配置失败", busy="")
            return False

        self._set(phase="启动内核", busy="启动中")
        if not self.kernel.start():
            self._set(phase="内核启动失败", error=self.kernel.last_error, busy="")
            self.notify(self.kernel.last_error)
            return False

        self._set(kernel_ok=True, error="", started_at=time.time())
        self._apply_saved_selection()
        self._apply_mode(self.settings.get("mode", "rule"))

        if self.settings.get("system_proxy", True):
            # 开机/刚连上 WiFi 时，校园网这类网络往往还没过门户认证。
            # 这时候接管系统代理等于把浏览器锁死：认证页自己都要走代理，
            # 而代理指向的内核此刻一个节点都不通。先探一下，不通就先让路，
            # 通了以后 _loop_health 会自动接回来。
            blocked, why = netguard.portal_status()
            if blocked:
                log.warning("网络尚未就绪（%s），本次先不接管系统代理", why)
                self._set(sysproxy=False, sysproxy_hold=True)
                self.notify(netguard.portal_hint(why))
            else:
                self.toggle_system_proxy(True, silent=True)

        self.refresh_now()
        self._sync_ai_node()
        self._start_loops()
        self._set(phase="运行中", busy="")
        log.info("启动完成，端口 %s", self.settings["mixed_port"])
        return True

    # ══════════ AI 专项（Claude Code / Codex）══════════
    def topo(self) -> dict[str, Any]:
        """链路拓扑：用户端 → 代理入口 → 落地出口。

        没测过出口 IP 时退一步，用节点的真实地址（订阅里的 server 域名解析
        到的那台机器）当终点，这样拓扑不会空着。
        """
        from . import geolink

        node = self.state.get("group_now") or ""
        pu = (self.state.get("purity") or {}).get(node) or {}
        local = geolink.own_ip()
        ent = geolink.entry(node) if node else None

        ext = geolink.exit_point(pu.get("ip") or "")
        kind = "exit"
        hint = ""
        if not ext:
            # ① 纯净度里有经纬度（查过 IP 画像）就直接用
            if pu.get("lat") is not None and pu.get("lon") is not None:
                ext = {
                    "ip": pu.get("ip") or "",
                    "city": pu.get("city") or "",
                    "cc": (pu.get("cc") or "").upper(),
                    "lat": pu.get("lat"),
                    "lon": pu.get("lon"),
                    "isp": pu.get("asn") or "",
                    "estimated": True,
                }
                kind = "node_ip"
                hint = "落地出口未实测，这条线画到节点 IP 的真实位置"
            # ② 退到入口机房本身（同一台机器就是节点地址）
            elif ent:
                ext = dict(ent, estimated=True)
                kind = "entry_ip"
                hint = "还没测出口 IP，先指向节点服务器地址；点「测出口 IP」可精确到落地城市"

        return {
            "node": node,
            "delay": (self.state.get("delays") or {}).get(node),
            "server": geolink.server_of(node),
            "points": {"local": local, "entry": ent, "exit": ext},
            "exit_kind": kind if ext else "",
            "ready": bool(local and ent and ext),
            "hint": hint or ("" if ext else "还没有落地 IP：先点节点列表的「测当前纯净度」或 AI 卡的「测出口 IP」"),
        }

    def ai_status(self) -> dict[str, Any]:
        """AI 稳定组的实时状态（便宜，可每轮轮询）。"""
        proxies = self.state.get("proxies") or {}
        g = proxies.get(configgen.AI_GROUP) or {}
        cur = g.get("now") or ""
        delays = self.state.get("delays") or {}
        pu = (self.state.get("purity") or {}).get(cur) or {}
        cc = (pu.get("cc") or "").upper()
        if not cc and cur:
            # 没测过纯净度时，退回到「节点名解析出的国家」，让时区提示也能用
            try:
                from .webui import _region_of  # 运行时导入，避开循环依赖
                cc = (_region_of(cur)[2] or "").upper()
            except Exception:  # noqa: BLE001
                cc = ""
        tz_bad, tz_msg = disguise.tz_risk(cc) if cc else (False, "")
        local_off = disguise.local_offset_hours()
        return {
            "enabled": bool(self.settings.get("ai_enabled", True)),
            "group": configgen.AI_GROUP,
            "node": cur,
            "delay": delays.get(cur),
            "purity": pu.get("score"),
            "purity_grade": pu.get("grade"),
            "exit_cc": cc,
            "exit_ip": pu.get("ip"),
            "utc8": cc in disguise.UTC8_CC,
            "tz_bad": tz_bad,
            "tz_msg": tz_msg,
            "sys_tz": f"UTC{local_off:+g}" if local_off is not None else "",
            "node_tz": (f"UTC{disguise.tz_offset_hours(cc):+g}"
                        if disguise.tz_offset_hours(cc) is not None else ""),
            "domains": len(configgen.AI_DOMAINS),
            "members": len(g.get("all") or []),
            "recommended": disguise.RECOMMENDED[:6],
        }

    def set_ai_node(self, name: str) -> tuple[bool, str]:
        name = (name or "").strip()
        if not name:
            return False, "缺少节点名"
        self.settings["ai_node"] = name
        self.settings.save()
        try:
            self.api.select(configgen.AI_GROUP, name)
        except ApiError as e:
            return False, f"切换失败：{e}"
        self.notify(f"AI 专用节点：{name}")
        self.refresh_now()
        return True, ""

    def _sync_ai_node(self) -> None:
        """首次启用时，把 AI 组钉到当前主组节点上，避免它空着。"""
        if not self.settings.get("ai_enabled", True):
            return
        if self.settings.get("ai_node"):
            try:
                self.api.select(configgen.AI_GROUP, self.settings["ai_node"])
            except ApiError:
                pass
            return
        cur = self.state.get("group_now")
        if cur:
            self.set_ai_node(cur)

    def set_ai_enabled(self, on: bool) -> tuple[bool, str]:
        self.settings["ai_enabled"] = bool(on)
        self.settings.save()
        cached = subscription.load_cache()
        if not cached:
            return False, "没有订阅缓存"
        sub = subscription.normalize(cached)
        cfg = configgen.build(sub, self.settings)
        path = configgen.write(cfg)
        if self.kernel.running():
            try:
                self.api.reload(path)
            except ApiError as e:
                return False, f"重载失败：{e}"
            self._apply_saved_selection()
            if on:
                self._sync_ai_node()
            self.refresh_now()
        self.notify("已启用 AI 专用通道" if on else "已关闭 AI 专用通道")
        return True, ""

    def subscriptions(self) -> list[dict[str, Any]]:
        """订阅列表（老版本只有 sub_url，这里自动迁移成一条）。"""
        subs = self.settings.get("subs") or []
        if not isinstance(subs, list):
            subs = []
        if not subs:
            url = (self.settings.get("sub_url") or "").strip()
            if url:
                subs = [{"name": "订阅 1", "url": url, "info": {}, "nodes": 0, "updated": 0}]
                self.settings["subs"] = subs
                self.settings["sub_active"] = 0
                self.settings.save()
        return subs

    def active_sub(self) -> dict[str, Any]:
        subs = self.subscriptions()
        if not subs:
            return {}
        try:
            i = int(self.settings.get("sub_active") or 0)
        except (TypeError, ValueError):
            i = 0
        if not 0 <= i < len(subs):
            i = 0
            self.settings["sub_active"] = 0
        return subs[i]

    # ══════════ 合并模式（同一机场多个账号，共用一套节点名）══════════
    SUB_LEFT_MIN = 0.3      # 剩余不足 0.3 GB 视为耗尽

    def sub_mode(self) -> str:
        return "merge" if self.settings.get("sub_mode") == "merge" else "single"

    def set_sub_mode(self, mode: str) -> tuple[bool, str]:
        mode = "merge" if mode == "merge" else "single"
        self.settings["sub_mode"] = mode
        self.settings.save()
        log.info("订阅模式 -> %s", mode)
        if mode == "merge":
            self._spawn("sub-info", self.refresh_sub_infos)
            self._spawn("sub-health", self.check_sub_health)
        self.notify("已开启合并模式：一个账号跑完自动换下一个" if mode == "merge"
                    else "已关闭合并模式")
        return True, ""

    @staticmethod
    def _sub_usable(sub: dict[str, Any]) -> tuple[bool, str]:
        """这条订阅还能不能用：(可用?, 原因)"""
        info = sub.get("info") or {}
        left = info.get("traffic_left_gb")
        days = info.get("days_left")
        if days is not None and days <= 0:
            return False, "已过期"
        if left is not None and left <= App.SUB_LEFT_MIN:
            return False, "流量已用尽"
        # 既没节点数、又读不到流量 → 这条多半是链接失效了
        if not sub.get("nodes") and left is None:
            return False, "还没拉到节点"
        return True, ""

    def sub_pool(self) -> list[dict[str, Any]]:
        """合并模式下的候选订阅，剩余流量多的排前面。"""
        out = []
        for i, s in enumerate(self.subscriptions()):
            ok, why = self._sub_usable(s)
            if not ok:
                continue
            left = (s.get("info") or {}).get("traffic_left_gb")
            out.append({"i": i, "sub": s, "left": left if left is not None else 1e9})
        out.sort(key=lambda x: x["left"], reverse=True)
        return out

    def refresh_sub_infos(self) -> int:
        """把每条订阅的流量/到期都刷一遍（只读订阅响应头，不下载配置）。

        合并模式下顶上显示的是总和，所以每条都得是新的；顺便也能发现
        「某条已经 0 流量了」，比等节点连不上要早。
        """
        subs = self.subscriptions()
        if not subs:
            return 0
        ua = self.settings.get("sub_ua", "clash.meta")
        port = int(self.settings.get("mixed_port", 7891))
        changed = 0
        for i, s in enumerate(subs):
            url = (s.get("url") or "").strip()
            if not url:
                continue
            info_new = subscription.fetch_userinfo(url, ua, port)
            if not info_new:
                continue
            info = dict(s.get("info") or {})
            for k, v in info_new.items():
                if v is not None:
                    info[k] = v
            if info != (s.get("info") or {}):
                subs[i]["info"] = info
                changed += 1
        if changed:
            self.settings["subs"] = subs
            self.settings.save()
            log.info("已刷新 %d 条订阅的流量信息", changed)
        return changed

    def check_sub_health(self) -> bool:
        """当前订阅流量耗尽/过期时，自动漂到还有余额的那条。

        同一机场的节点名是一致的，所以切过去之后能把用户原来选的节点
        原样选回来 —— 体感就是「什么都没变，只是又通了」。
        """
        if self.sub_mode() != "merge":
            return False
        subs = self.subscriptions()
        if len(subs) < 2:
            return False
        try:
            cur = int(self.settings.get("sub_active") or 0)
        except (TypeError, ValueError):
            cur = 0
        if not 0 <= cur < len(subs):
            cur = 0
        ok, why = self._sub_usable(subs[cur])
        if ok:
            return False
        pool = self.sub_pool()
        nxt = next((p["i"] for p in pool if p["i"] != cur), None)
        if nxt is None:
            if not ok:
                log.warning("合并模式：所有订阅都不可用（%s）", why)
            return False
        log.info("合并模式：%s（%s）→ 切到 %s", subs[cur].get("name"), why or "手动检查",
                 subs[nxt].get("name"))
        self.notify(f"{subs[cur].get('name')} {why or '不可用'}，已自动切到 {subs[nxt].get('name')}")
        self.use_subscription(nxt, keep_node=True)
        return True

    def set_subscription(self, url: str, name: str = "") -> tuple[bool, str]:
        """存订阅链接并立刻切过去（同一个链接不会重复添加）。"""
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return False, "订阅链接必须以 http:// 或 https:// 开头"
        subs = self.subscriptions()
        for i, s in enumerate(subs):
            if (s.get("url") or "").strip() == url:
                self.settings["sub_active"] = i
                break
        else:
            label = (name or "").strip() or f"订阅 {len(subs) + 1}"
            subs.append({"name": label, "url": url, "info": {}, "nodes": 0, "updated": 0})
            self.settings["sub_active"] = len(subs) - 1
        self.settings["subs"] = subs
        self.settings["sub_url"] = url
        self.settings["sub_hash"] = ""
        self.settings.save()
        self._set(busy="保存订阅", error="")
        try:
            if self.kernel.running():
                ok = self.update_subscription(force=True)
                return ok, "" if ok else (self.state.get("sub_error") or "订阅拉取失败")

            # 内核没跑（首次配置）：走完整启动流程
            ok = self.start()
            if not ok:
                return False, (self.state.get("error") or "启动失败")
            return True, ""
        finally:
            self._set(busy="")

    def use_subscription(self, idx: int, keep_node: bool = False) -> tuple[bool, str]:
        subs = self.subscriptions()
        if not 0 <= idx < len(subs):
            return False, "订阅不存在"
        sub = subs[idx]
        # 同一机场换账号时节点名是通的，记住原来那个，切完再选回去
        prev = (self.state.get("group_now") or "") if keep_node else ""
        self.settings["sub_active"] = idx
        self.settings["sub_url"] = sub.get("url") or ""
        self.settings["sub_hash"] = ""
        self.settings.save()
        log.info("切换订阅 -> %s", sub.get("name"))
        self._set(busy="切换订阅", error="")
        try:
            ok = self.update_subscription(force=True)
            if ok and prev:
                try:
                    cached = subscription.load_cache() or {}
                    names = {p.get("name") for p in (cached.get("proxies") or [])}
                except Exception:  # noqa: BLE001
                    names = set()
                if prev in names:
                    try:
                        self.api.select(self.settings.get("main_group") or "节点选择", prev)
                        log.info("切订阅后恢复节点：%s", prev)
                    except Exception as e:  # noqa: BLE001
                        log.debug("恢复节点失败: %s", e)
            return ok, "" if ok else (self.state.get("sub_error") or "订阅拉取失败")
        except Exception as e:  # noqa: BLE001
            log.exception("切换订阅异常")
            return False, f"切换失败：{e}"
        finally:
            self._set(busy="")

    def remove_subscription(self, idx: int) -> tuple[bool, str]:
        # 用 url 定位而不是下标：并发的 update_subscription 可能刚把列表重排过，
        # 下标会指到另一条上，删错订阅是不可逆的。
        with self._lock:
            subs = [dict(s) for s in self.subscriptions()]
            if not 0 <= idx < len(subs):
                return False, "订阅不存在"
            if len(subs) == 1:
                return False, "至少留一个订阅"
            gone = subs.pop(idx)
            gone_url = (gone.get("url") or "").strip()
            was_active = idx == _int(self.settings.get("sub_active"), 0)
            self.settings["subs"] = subs
            if was_active:
                cur = 0
            else:
                cur = min(_int(self.settings.get("sub_active"), 0), len(subs) - 1)
                if cur < 0:
                    cur = 0
                # 前面的条目被删掉时下标要跟着前移
                if idx < _int(self.settings.get("sub_active"), 0):
                    cur = max(0, _int(self.settings.get("sub_active"), 0) - 1)
            self.settings["sub_active"] = cur
            self.settings["sub_url"] = (subs[cur].get("url") or "") if subs else ""
            self.settings["sub_hash"] = ""
            try:
                self.settings.save()
            except Exception as e:  # noqa: BLE001
                log.warning("保存订阅列表失败: %s", e)
                return False, "保存失败"
        log.info("删除订阅 %s", gone.get("name"))
        if was_active or self.kernel.running():
            self._spawn("sub-update", lambda: self.update_subscription(True))
        return True, ""

    def shutdown(self) -> None:
        log.info("开始退出流程")
        self._stop.set()
        if self.settings.get("system_proxy", True):
            try:
                sysproxy.disable()
            except Exception as e:  # noqa: BLE001
                log.warning("还原系统代理失败: %s", e)
        self.kernel.stop()
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()

    # ══════════ 订阅 & 配置 ══════════
    def prepare_config(self, initial: bool = False) -> bool:
        """确保有可用配置；没有缓存订阅就先下载。"""
        if not SUB_CACHE.exists():
            if not self.update_subscription(force=True, write_config=False):
                self._set(error=self.state["sub_error"] or "订阅拉取失败")
                return False
        cached = subscription.load_cache()
        if not cached:
            self._set(error="订阅缓存不可用")
            return False
        sub = subscription.normalize(cached)
        cfg = configgen.build(sub, self.settings)
        configgen.write(cfg)
        self._set(
            main_group=cfg["rules"][-1].split(",", 1)[1],
            sub_nodes=len(sub["proxies"]),
            info_nodes=list(sub.get("info") or []),
        )
        return True

    def update_subscription(self, force: bool = False, write_config: bool = True) -> bool:
        url = (self.active_sub() or {}).get("url") or self.settings.get("sub_url", "")
        if not url:
            self._set(sub_error="未配置订阅链接")
            return False
        self._set(busy="更新订阅")
        try:
            raw = subscription.fetch(
                url,
                self.settings.get("sub_ua", "clash.meta"),
                int(self.settings["mixed_port"]),
            )
            parsed = subscription.parse(raw)
            new_hash = subscription.save_cache(raw, parsed)
            changed = new_hash != self.settings.get("sub_hash")
            self.settings["sub_hash"] = new_hash

            # 回填这条订阅的流量/到期/节点数，左上角和订阅列表都靠它
            info_names = [p.get("name") for p in (parsed.get("proxies") or [])
                          if p.get("name") and subscription.is_info_node(p["name"])]
            detail = subscription.parse_info([n for n in info_names if n])
            with self._lock:
                subs = [dict(s) for s in self.subscriptions()]
                i = _int(self.settings.get("sub_active"), 0)
                if not 0 <= i < len(subs):
                    i = 0 if subs else -1
                if i >= 0:
                    # 这次没解析出内容（机场换了格式/临时返回空）就别把好数据抹掉，
                    # 否则左上角和订阅列表会一起变空。
                    if detail:
                        subs[i]["info"] = detail
                    subs[i]["nodes"] = len([p for p in (parsed.get("proxies") or [])
                                            if p.get("name") and not subscription.is_info_node(p["name"])])
                    subs[i]["updated"] = int(time.time())
                    subs[i]["url"] = url
                self.settings["subs"] = subs
                self.settings.save()
            with self._lock:
                self.state["sub_last_ok"] = time.time()
                self.state["sub_last_try"] = time.time()
                self.state["sub_error"] = ""
            log.info("订阅已更新（%s），%d 个节点", "内容变化" if changed else "内容相同", len(parsed["proxies"]))

            if write_config and changed and self.kernel.running():
                sub = subscription.normalize(parsed)
                cfg = configgen.build(sub, self.settings)
                path = configgen.write(cfg)
                self.api.reload(path)
                self._apply_saved_selection()
                self.refresh_now()
                self.notify(f"订阅已更新：{len(sub['proxies'])} 个节点")
            self._set(busy="")
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("订阅更新失败: %s", e)
            with self._lock:
                self.state["sub_error"] = str(e)[:300]
                self.state["sub_last_try"] = time.time()
            self._set(busy="")
            return False

    # ══════════ 节点 / 模式 ══════════
    def select_node(self, name: str) -> bool:
        group = self.settings.get("main_group") or "节点选择"
        if self._purity_running or self._speed_running:
            self.notify("正在检测中，完成后再切换节点")
            return False
        # 「自动选择」这类 url-test 组会自己漂到失效节点上，
        # 手动点它时改成「测速 + 验证后落到具体节点」。
        if name in {"自动选择", "自动切换"}:
            self._bg_auto = threading.Thread(target=self.auto_pick_best, daemon=True)
            self._bg_auto.start()
            return True
        try:
            self.api.select(group, name)
        except ApiError as e:
            log.warning("切换节点失败: %s", e)
            self.notify(f"切换失败：{e}")
            return False
        sel = dict(self.settings.get("selected") or {})
        sel[group] = name
        self.settings["selected"] = sel
        self.settings.save()
        self._set(busy="")
        log.info("已切换到 %s", name)
        self.notify(f"当前节点：{name}")
        self.refresh_now()
        self._probe_selected(name)
        return True

    def _probe_selected(self, name: str) -> None:
        """切完节点顺手探一次出口 IP/城市，好让列表和卡片直接显示从哪出去。"""
        known = (self.state.get("purity") or {}).get(name) or {}
        if known.get("ip") and known.get("city"):
            return
        if not self.settings.get("probe_on_select", True):
            return

        def run():
            time.sleep(1.5)          # 等连接真正建起来
            if self._purity_running or self._speed_running:
                return
            try:
                self.check_purity([name], 0)
            except Exception as e:  # noqa: BLE001
                log.debug("切节点后探测出口失败: %s", e)

        threading.Thread(target=run, daemon=True).start()

    def _apply_saved_selection(self) -> None:
        group = self.settings.get("main_group") or "节点选择"
        try:
            proxies = self.api.proxies()
        except ApiError:
            return
        g = proxies.get(group, {})
        all_nodes = g.get("all") or []
        saved = (self.settings.get("selected") or {}).get(group)

        if saved and saved in all_nodes:
            try:
                self.api.select(group, saved)
                log.info("恢复上次节点: %s", saved)
            except ApiError as e:
                log.info("恢复节点失败: %s", e)
            return

        # 首次运行：挑一个具体节点先顶上，真正的可用性验证由 boot-verify 完成
        prefer = next(
            (n for n in all_nodes
             if n not in ("DIRECT", "REJECT", "PASS", "自动选择", "自动切换")),
            None,
        )
        if prefer:
            try:
                self.api.select(group, prefer)
                sel = dict(self.settings.get("selected") or {})
                sel[group] = prefer
                self.settings["selected"] = sel
                self.settings.save()
                log.info("默认节点: %s", prefer)
            except ApiError as e:
                log.info("设置默认节点失败: %s", e)

    def set_mode(self, mode: str) -> None:
        mode = mode if mode in {"rule", "global", "direct"} else "rule"
        try:
            self.api.set_mode(mode)
        except ApiError as e:
            self.notify(f"切换模式失败：{e}")
            return
        self.settings["mode"] = mode
        self.settings.save()
        self._set(mode=mode)
        self.notify({"rule": "规则分流", "global": "全局代理", "direct": "全部直连"}[mode])

    def _apply_mode(self, mode: str) -> None:
        try:
            self.api.set_mode(mode)
            self._set(mode=mode)
        except ApiError as e:
            log.info("应用模式失败: %s", e)

    # ══════════ 状态刷新 ══════════
    def refresh_now(self) -> None:
        if not self.kernel.running():
            self._set(kernel_ok=False)
            return
        try:
            proxies = self.api.proxies()
        except ApiError as e:
            log.debug("读取 proxies 失败: %s", e)
            self._set(kernel_ok=False)
            return

        group = self.settings.get("main_group") or "节点选择"
        g = proxies.get(group, {})
        all_nodes = list(g.get("all") or [])
        now = g.get("now") or ""

        traffic = dict(self.state["traffic"])
        conns = 0
        try:
            c = self.api.connections()
            up = int(c.get("uploadTotal") or 0)
            down = int(c.get("downloadTotal") or 0)
            conns = len(c.get("connections") or [])
            t = time.time()
            lu, ld, lt = self._last_conn
            if lt and t > lt:
                dt = t - lt
                traffic = {
                    "up": up,
                    "down": down,
                    "up_speed": max(0, int((up - lu) / dt)),
                    "down_speed": max(0, int((down - ld) / dt)),
                }
            else:
                traffic.update({"up": up, "down": down})
            self._last_conn = (up, down, t)
        except ApiError:
            pass

        # 注册表读取可能因策略限制/只读 HKCU 抛异常；它绝不能拖垮整块 state 更新，
        # 否则 UI 会永远停在「启动中」。
        try:
            sp = sysproxy.is_enabled_to(f"127.0.0.1:{self.settings.get('mixed_port')}")
        except Exception as e:  # noqa: BLE001
            log.debug("读取系统代理状态失败: %s", e)
            sp = bool(self.state.get("sysproxy"))

        with self._lock:
            self.state.update(
                kernel_ok=True,
                error="",
                group_all=all_nodes,
                group_now=now,
                proxies=proxies,
                traffic=traffic,
                connections=conns,
                sysproxy=sp,
            )
        self._changed()

    # ══════════ 测速 ══════════
    def test_delays(self, group: str | None = None) -> dict[str, int]:
        group = group or (self.settings.get("main_group") or "节点选择")
        url = self.settings.get("delay_url", "https://www.gstatic.com/generate_204")
        timeout = int(self.settings.get("delay_timeout_ms", 5000))
        self._set(busy="测速中")
        self._set_progress("delay", 0, 0, "刷新延迟…")
        delays: dict[str, int] = {}
        try:
            delays = self.api.group_delay(group, url, timeout)
        except ApiError as e:
            log.warning("测速失败: %s", e)
        except Exception:  # noqa: BLE001
            log.exception("测速异常")
        finally:
            # busy 必须无条件清掉：一旦漏在这里，_wait_idle 会永远返回 False，
            # 之后所有「测纯净度 / 带宽测速」都会被静默跳过，面板上毫无提示。
            self._set_progress()
            with self._lock:
                self.state["delays"] = delays
                self._delays_ts = time.time()
            self._set(busy="")
            self._changed()
        if delays:
            best = min(delays.items(), key=lambda kv: kv[1])
            log.info("测速完成 %d 个节点，最快 %s %dms", len(delays), best[0], best[1])
        return delays

    def _recent_delays(self, max_age: float = 120.0) -> dict[str, int]:
        with self._lock:
            ts = getattr(self, "_delays_ts", 0.0)
            d = dict(self.state.get("delays") or {})
        return d if d and (time.time() - ts) <= max_age else {}

    # ══════════ IP 纯净度 / 带宽 ══════════
    def _probe_targets(self, names: list[str] | None = None, limit: int = 0,
                       include_dead: bool = False) -> list[str]:
        """挑出可以检测的节点（排除策略组、DIRECT、信息行）。

        include_dead=True 时连「测速没通过」的节点也一起测（用户要求全测时用）。
        """
        proxies = self.state.get("proxies") or {}
        delays = self.state.get("delays") or {}
        group_types = {"selector", "urltest", "fallback", "loadbalance"}
        pool = [
            n for n in (self.state.get("group_all") or [])
            if (proxies.get(n) or {}).get("type", "").lower() not in group_types
            and n not in ("DIRECT", "REJECT")
        ]
        if names:
            pool = [n for n in pool if n in names]
        elif include_dead:
            pool = sorted(pool, key=lambda n: (delays.get(n) is None, delays.get(n) or 99999))
        else:
            alive = [n for n in pool if delays.get(n)]
            pool = sorted(alive, key=lambda n: delays[n]) if alive else pool
        return pool[:limit] if limit and limit > 0 else pool

    def _wait_idle(self, self_task: str = "", timeout: float = 25.0) -> bool:
        """等检测类长任务（纯净度/测速）结束。

        判据必须是真实旗标，不能是 state["busy"]：那只是一句给人看的文案，
        任何任务收尾都会把它清空 —— 包括不是自己的。两个任务同时拿它当锁，
        就会双双判「现在没人占」然后一起切节点，测出来的出口 IP 记到错节点上。

        self_task 传自己的名字。调用方在进来之前已经把自己那个旗标置 True 了，
        不排掉就会自己等自己，一路等到超时，任务永远发不出去。
        """
        running = {"purity": self._purity_running, "speed": self._speed_running}
        running.pop(self_task, None)
        end = time.time() + timeout
        while time.time() < end:
            if self._stop.is_set():
                return False
            if not any(running.values()):
                return True
            # 期间别的任务可能刚好收尾，重新读一次，别拿进函数时的旧值死等
            running["purity"] = self._purity_running
            running["speed"] = self._speed_running
            running.pop(self_task, None)
            time.sleep(0.4)
        log.warning("等待空闲超时（纯净度=%s 测速=%s 自己=%s）",
                    self._purity_running, self._speed_running, self_task or "-")
        return False

    def check_purity(self, names: list[str] | None = None, limit: int = 0,
                     include_dead: bool = False) -> int:
        """逐个节点切换后查询出口 IP 画像，返回成功数量。

        必须串行：同一时刻只有一个节点生效，无法并发。
        """
        if not self._claim_task("_purity_running"):
            self.notify("已有检测任务在跑")
            return 0
        if not self.kernel.running():
            self._purity_running = False
            self.notify("内核未运行，无法检测")
            return 0

        targets = self._probe_targets(names, limit, include_dead)
        if not targets:
            self._purity_running = False
            self.notify("没有可检测的节点")
            return 0

        if not self._wait_idle("purity"):
            self._purity_running = False
            self.notify("当前有任务在运行，稍后再试")
            return 0

        group = self.settings.get("main_group") or "节点选择"
        origin = self.state.get("group_now") or ""
        port = int(self.settings["mixed_port"])
        ok = 0
        fail = 0
        total = len(targets)
        log.info("开始纯净度检测：%d 个节点", total)
        try:
            for i, name in enumerate(targets, 1):
                if self._stop.is_set():
                    log.info("检测被中断（程序要退出）")
                    break
                self._set(busy=f"纯净度 {i}/{total}")
                self._set_progress("purity", i, total, f"检测 {i}/{total}")
                # 单个节点出任何问题都不能拖垮整轮
                try:
                    self.api.select(group, name)
                    # 切完立刻打会碰上「新节点还在建连」；连续切换时前一个节点的
                    # 连接也还没拆干净，实测留 1.2s 成功率明显更高
                    time.sleep(1.2)
                    info = purity.fetch_exit_info(port, timeout=8.0)
                    if not info:
                        # 再给一次机会：有些落地节点首包慢，重试基本能拿到
                        time.sleep(2.5)
                        info = purity.fetch_exit_info(port, timeout=9.0)
                    if not info:
                        fail += 1
                        log.info("检测 %d/%d %s 查询失败（可能不通）", i, total, name)
                        continue
                    sc = purity.score(info, name)
                    purity.put(name, sc)
                    with self._lock:
                        self.state["purity"][name] = sc
                    ok += 1
                    log.info("检测 %d/%d %s -> %s %s 分", i, total, name,
                             str((sc or {}).get("ip") or ""), (sc or {}).get("score"))
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    log.exception("检测 %d/%d %s 出错: %s: %s", i, total, name, type(e).__name__, e)
                    continue
                self._changed()
        except BaseException as e:  # noqa: BLE001
            log.exception("纯净度检测循环异常终止: %s", e)
        finally:
            self._set_progress()
            if origin:
                try:
                    self.api.select(group, origin)
                except ApiError:
                    pass
            self._purity_running = False
            self._set(busy="")
            self.refresh_now()
        log.info("纯净度检测完成：%d 成功 / %d 失败", ok, fail)
        if ok:
            self.notify(f"纯净度检测完成（{ok} 个节点）")
        elif fail:
            self.notify("检测失败：节点连不通，先测速或换个节点")
        return ok

    def check_speed(self, names: list[str] | None = None, limit: int = 10) -> int:
        """下载测速（默认延迟最好的 10 个节点，每个 5MB）。"""
        if not self._claim_task("_speed_running"):
            self.notify("已有检测任务在跑")
            return 0
        if not self.kernel.running():
            self._speed_running = False
            self.notify("内核未运行，无法测速")
            return 0

        targets = self._probe_targets(names, limit)
        if not targets:
            self._speed_running = False
            self.notify("没有可测速的节点")
            return 0

        if not self._wait_idle("speed"):
            self._speed_running = False
            self.notify("当前有任务在运行，稍后再试")
            return 0

        group = self.settings.get("main_group") or "节点选择"
        origin = self.state.get("group_now") or ""
        port = int(self.settings["mixed_port"])
        ok = 0
        total = len(targets)
        log.info("开始带宽测速：%d 个节点", total)
        try:
            for i, name in enumerate(targets, 1):
                if self._stop.is_set():
                    break
                self._set(busy=f"测速 {i}/{total}")
                self._set_progress("speed", i, total, f"测速 {i}/{total}")
                try:
                    self.api.select(group, name)
                except ApiError:
                    continue
                time.sleep(0.18)
                res = purity.measure_speed(port, timeout=20.0)
                if not res:
                    continue
                with self._lock:
                    self.state["speed"][name] = res
                ok += 1
                self._changed()
        finally:
            self._set_progress()
            if origin:
                try:
                    self.api.select(group, origin)
                except ApiError:
                    pass
            self._speed_running = False
            self._set(busy="")
            self.refresh_now()
        log.info("带宽测速完成：%d/%d 成功", ok, len(targets))
        if ok:
            self.notify(f"测速完成（{ok} 个节点）")
        return ok

    def auto_pick_best(self) -> str | None:
        """测速 → 按延迟排序 → 逐个做真实 HTTP 验证 → 选中第一个真能上网的节点。

        只测速是不够的：机场节点经常「延迟好看但 TCP/TLS 握不上」，
        直接用会表现为「自动选择」一会儿能上一会儿上不了。
        """
        group = self.settings.get("main_group") or "节点选择"
        delays = self._recent_delays() or self.test_delays(group) or (self.state.get("delays") or {})
        allowed = set(self.state.get("group_all") or [])
        proxies = self.state.get("proxies") or {}
        group_types = {"selector", "urltest", "fallback", "loadbalance"}
        cand = sorted(
            (k for k, v in delays.items()
             if k in allowed and v and v > 0
             and (proxies.get(k, {}).get("type", "").lower() not in group_types)),
            key=lambda k: delays[k],
        )[:8]
        if not cand:
            return None
        before = self.state.get("group_now")   # 验证过程里 group_now 会被改写，先留底
        self._set(busy="验证节点")
        try:
            best = self._verify_and_select(cand)
        finally:
            self._set(busy="")
        if best:
            log.info("已选中可用节点: %s", best)
            if best != before:
                self.notify(f"已切换到可用节点：{best}")
        return best

    def _verify_and_select(self, candidates: list[str], timeout: float = 6.0) -> str | None:
        """逐个验证候选节点：连发两次真实请求都成功才算可用。

        机场节点会「刚测通 2 秒后就掉」，单次验证选出来的节点经常立刻不可用。
        """
        group = self.settings.get("main_group") or "节点选择"
        for name in candidates:
            try:
                self.api.select(group, name)
            except ApiError as e:
                log.debug("选择 %s 失败: %s", name, e)
                continue
            time.sleep(0.25)
            if not self._probe_via_proxy(timeout):
                log.info("节点 %s 首次验证失败", name)
                continue
            time.sleep(0.6)
            if not self._probe_via_proxy(timeout):
                log.info("节点 %s 二次验证失败（抖动）", name)
                continue
            sel = dict(self.settings.get("selected") or {})
            sel[group] = name
            self.settings["selected"] = sel
            self.settings.save()
            self._set(group_now=name)
            return name
        return None

    def _probe_via_proxy(self, timeout: float = 6.0) -> bool:
        """通过本地代理端口做一次真实 HTTPS 请求，确认整条链路可用。"""
        port = int(self.settings["mixed_port"])
        p = f"http://127.0.0.1:{port}"
        s = requests.Session()
        s.trust_env = False
        s.proxies = {"http": p, "https": p}
        for url in ("https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/generate_204"):
            try:
                r = s.get(url, timeout=timeout, allow_redirects=False)
                if r.status_code in (200, 204):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def ensure_usable(self) -> str | None:
        """当前节点还活着就什么都不做，挂了才重新挑。"""
        if not self.kernel.running():
            return None
        if self._probe_via_proxy(4.0):
            return self.state.get("group_now")
        log.warning("当前节点不可用，开始重新选择")
        return self.auto_pick_best()

    # ══════════ 系统代理 / 自启 ══════════
    def toggle_system_proxy(self, on: bool, silent: bool = False) -> None:
        server = f"127.0.0.1:{self.settings.get('mixed_port')}"
        if on and not silent:
            # 手动点开关时也拦一道：校园网没过认证就接管，
            # 用户会立刻打不开任何网页，连认证页都进不去 —— 这比「点了没反应」糟得多
            blocked, why = netguard.portal_status()
            if blocked:
                hint = ("现在多半还没登录校园网。先把登录页打开、登录成功，"
                        "再回来开这个开关（程序也会自动接上）") if why == "captive" else \
                       "现在网络还没通。等能正常打开网页了再开这个开关（程序也会自动接上）"
                log.warning("拒绝接管系统代理：%s", why)
                self.notify(hint)
                self._set(sysproxy=False, sysproxy_hold=True)
                return
        # 先把意图落盘再动注册表：顺序反了的话，注册表改完、save 抛异常，
        # settings 里还是旧值，退出时按旧值判断就漏还原 → 用户断网。
        prev = bool(self.settings.get("system_proxy", False))
        self.settings["system_proxy"] = bool(on)
        try:
            self.settings.save()
        except Exception as e:  # noqa: BLE001
            self.settings["system_proxy"] = prev
            log.warning("保存系统代理开关失败: %s", e)
            self.notify(f"保存设置失败：{e}")
            return
        try:
            if on:
                sysproxy.enable(server)
            else:
                sysproxy.disable()
        except Exception as e:  # noqa: BLE001
            # 注册表没改成功：把 settings 回滚，保持两边一致
            self.settings["system_proxy"] = prev
            try:
                self.settings.save()
            except Exception:  # noqa: BLE001
                pass
            log.warning("系统代理操作失败: %s", e)
            self.notify(f"系统代理操作失败：{e}")
            return
        self._set(sysproxy=bool(on), sysproxy_hold=False)
        if not silent:
            self.notify("系统代理已开启" if on else "系统代理已关闭（浏览器等不再走代理）")

    def set_autostart(self, on: bool) -> None:
        try:
            real = autostart.toggle(bool(on))
        except Exception as e:  # noqa: BLE001
            self.notify(f"设置开机自启失败：{e}")
            return
        self.settings["autostart"] = real
        self.settings.save()
        self._set(autostart=real)
        self.notify("已设置开机自启" if real else "已取消开机自启")

    # ══════════ 杂项 ══════════
    def open_dashboard(self) -> None:
        base = f"http://127.0.0.1:{self.settings['controller_port']}"
        url = f"{base}/ui/" if (UI_DIR / "index.html").exists() else base
        webbrowser.open(url)

    def open_log(self) -> None:
        import os

        os.startfile(str(LOGS))  # noqa: S606

    def close_connections(self) -> None:
        try:
            self.api.close_all_connections()
            self.notify("已断开全部连接")
        except ApiError as e:
            self.notify(f"操作失败：{e}")

    def restart_kernel(self) -> None:
        self._set(busy="重启内核")
        ok = False
        err = ""
        try:
            self.kernel.stop()
            ok = self.kernel.start()
            if not ok:
                err = self.kernel.last_error or ""
        except Exception as e:  # noqa: BLE001
            log.exception("重启内核异常")
            err = f"重启内核失败：{e}"
        finally:
            # busy 漏清会让所有后续任务被 _wait_idle 永久挡住
            self._set(busy="", kernel_ok=ok, error=err)
        if ok:
            try:
                self._apply_saved_selection()
            except Exception as e:  # noqa: BLE001
                log.warning("恢复节点选择失败: %s", e)
        self.refresh_now()

    # ══════════ 后台线程 ══════════
    def _start_loops(self) -> None:
        if self._loops_started:
            return
        self._loops_started = True
        self._spawn("refresh", self._loop_refresh)
        if int(self.settings.get("delay_interval_min", 20)) > 0:
            self._spawn("delay", self._loop_delay)
        self._spawn("health", self._loop_health)
        self._spawn("heal", self._loop_heal)
        if int(self.settings.get("sub_update_hours", 6)) > 0:
            self._spawn("sub", self._loop_sub)
        self._spawn("sub-pool", self._loop_sub_pool)
        self._spawn("boot-verify", self._boot_verify)
        if self.settings.get("auto_update", True):
            self._spawn("update", self._loop_update)

    def _loop_health(self) -> None:
        """每 20 秒做一次系统代理看护 + 每 45 秒轻量探测节点。"""
        tick = 0
        while not self._stop.wait(20):
            tick += 1
            # ① 系统代理看护：别的代理客户端会偷偷把注册表改回去
            if self.settings.get("system_proxy") and self.kernel.running():
                server = f"127.0.0.1:{self.settings['mixed_port']}"
                if not sysproxy.is_enabled_to(server):
                    # 抢回来之前必须先确认「现在接管是帮忙而不是添乱」。
                    # 校园网没认证时，代理被清掉恰恰是用户自己修的（或火绒修的），
                    # 我们每 20 秒抢一次，用户就永远进不了认证页。
                    blocked, why = netguard.portal_status()
                    if blocked:
                        if not self.state.get("sysproxy_hold"):
                            log.info("网络尚未就绪（%s），暂不接管系统代理", why)
                            self._set(sysproxy_hold=True, sysproxy=False)
                        continue
                    if self.state.get("sysproxy_hold"):
                        self._set(sysproxy_hold=False)
                    log.warning("系统代理被其它程序改动，重新接管回 %s", server)
                    try:
                        sysproxy.enable(server)
                        self._set(sysproxy=True)
                    except Exception as e:  # noqa: BLE001
                        log.warning("重新接管系统代理失败: %s", e)

            # ② 节点健康探测
            if tick % 2 or not self.settings.get("heal", True) or not self.kernel.running():
                continue
            if self.state.get("busy") or self._stop.is_set():
                continue  # 正在测速/切换，或正在退出，别干扰
            if self._probe_via_proxy(5.0):
                self._health_fail = 0
                continue
            self._health_fail = getattr(self, "_health_fail", 0) + 1
            log.warning("健康探测失败（连续 %d 次）", self._health_fail)
            if self._health_fail < 2:
                continue
            self._health_fail = 0
            try:
                picked = self.ensure_usable()
                if picked:
                    self.notify(f"节点已自动切换：{picked}")
            except Exception as e:  # noqa: BLE001
                log.warning("自动切换失败: %s", e)

    def _boot_verify(self) -> None:
        """启动后确认当前节点真能上网；不行就自动换个能用的。"""
        if self._stop.wait(2):
            return
        try:
            if self.settings.get("auto_best"):
                self.auto_pick_best()
            else:
                self.ensure_usable()
            self.refresh_now()
        except Exception as e:  # noqa: BLE001
            log.warning("启动自检异常: %s", e)

    def _spawn(self, name: str, fn) -> None:
        t = threading.Thread(target=fn, name=f"loop-{name}", daemon=True)
        t.start()
        self._threads.append(t)

    def _loop_refresh(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.refresh_now()
            except Exception as e:  # noqa: BLE001
                log.debug("刷新异常: %s", e)

    def _loop_delay(self) -> None:
        interval = max(1, int(self.settings.get("delay_interval_min", 20))) * 60
        while not self._stop.wait(8):
            try:
                self.test_delays()
            except Exception as e:  # noqa: BLE001
                log.debug("测速异常: %s", e)
            if self._stop.wait(interval):
                return

    def _loop_heal(self) -> None:
        """健康看护：auto_best 开就追最快，否则只在当前节点失效时换。"""
        interval = max(1, int(self.settings.get("auto_best_interval_min", 30))) * 60
        while not self._stop.wait(interval):
            try:
                if self.settings.get("auto_best"):
                    self.auto_pick_best()
                elif self.settings.get("heal", True):
                    self.ensure_usable()
            except Exception as e:  # noqa: BLE001
                log.debug("健康看护异常: %s", e)

    def _loop_sub(self) -> None:
        interval = max(1, int(self.settings.get("sub_update_hours", 6))) * 3600
        while not self._stop.wait(interval):
            try:
                self.update_subscription()
            except Exception as e:  # noqa: BLE001
                log.debug("订阅更新异常: %s", e)

    def _loop_sub_pool(self) -> None:
        """合并模式看门狗：启动后先查一次，之后每 3 分钟刷余额 + 看要不要换账号。"""
        wait = 18
        while not self._stop.wait(wait):
            wait = 180
            if self.sub_mode() != "merge":
                continue
            try:
                self.refresh_sub_infos()
            except Exception as e:  # noqa: BLE001
                log.debug("刷新订阅余额异常: %s", e)
            try:
                self.check_sub_health()
            except Exception as e:  # noqa: BLE001
                log.debug("合并模式巡检异常: %s", e)

    # ══════════ 自动更新 ══════════
    # ── 代理冲突清理 ───────────────────────────────────────────────────
    def _self_pids(self) -> set[int]:
        """自己的进程集合（含内核），扫描时排除 —— 否则一键清理会把自己杀掉。"""
        pids = {os.getpid()}
        pid = getattr(getattr(self.kernel, "proc", None), "pid", None)
        if pid:
            pids.add(int(pid))
        try:
            pids.add(int(os.getppid()))
        except Exception:  # noqa: BLE001
            pass
        return pids

    def net_scan(self) -> dict[str, Any]:
        """查一遍有没有别的代理软件在抢系统代理 / 开着 TUN。"""
        from . import netguard

        try:
            info = netguard.scan(exclude_pids=self._self_pids())
            info["is_admin"] = netguard.is_admin()
            info["ok"] = True
        except Exception as e:  # noqa: BLE001
            log.warning("扫描代理冲突失败: %s", e)
            info = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with self._lock:
            self.state["conflict"] = info
        self._changed()
        return info

    def net_fix(self, target: str, pids: list[int] | None = None,
                names: list[str] | None = None) -> dict[str, Any]:
        """target: sys（关系统代理）/ apps（结束进程）/ adapters（禁用网卡）。"""
        from . import netguard

        target = (target or "").strip()
        try:
            if target == "sys":
                # 关键顺序：先把「自己想开系统代理」的意图落盘关掉，
                # 否则健康看护线程 20 秒后会把代理又装回去，白清一场。
                self.settings["system_proxy"] = False
                try:
                    self.settings.save()
                except Exception as e:  # noqa: BLE001
                    log.warning("保存系统代理开关失败: %s", e)
                res = netguard.wipe_system_proxy()
                self._set(sysproxy=False)
                log.info("解除冲突：系统代理已清空 ok=%s", res.get("ok"))
            elif target == "apps":
                res = netguard.kill_apps([int(x) for x in (pids or [])])
                log.info("解除冲突：结束 %d 个进程", len(res.get("results") or []))
            elif target == "adapters":
                res = netguard.disable_adapters([str(x) for x in (names or [])])
                log.info("解除冲突：禁用 %d 张网卡", len(res.get("results") or []))
            else:
                return {"ok": False, "error": f"未知目标：{target}"}
        except Exception as e:  # noqa: BLE001
            log.exception("解除代理冲突失败")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        after = self.net_scan()
        return {"ok": bool(res.get("ok", True)), "detail": res, "scan": after}

    # ── 反馈 / 用户协议 ────────────────────────────────────────────────
    # ——— 实时连接 ———

    def connections(self, limit: int = 200) -> dict[str, Any]:
        """当前所有连接，按流量降序。

        内核自己会把 /connections 回成一坨（含每条连接的完整元数据），
        前端只需要「谁在用、走了哪个节点、用了多少流量」，所以这里先削一刀。
        """
        if not self.kernel.running():
            return {"ok": False, "error": "内核没在运行", "items": [], "down": 0, "up": 0}
        try:
            raw = self.api.connections()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "items": [], "down": 0, "up": 0}

        conns = raw.get("connections") if isinstance(raw.get("connections"), list) else []
        items: list[dict[str, Any]] = []
        for c in conns:
            if not isinstance(c, dict):
                continue
            meta = c.get("metadata") if isinstance(c.get("metadata"), dict) else {}
            up = _num(c.get("upload"))
            down = _num(c.get("download"))
            chains = c.get("chains") if isinstance(c.get("chains"), list) else []
            items.append({
                "id": str(c.get("id") or ""),
                "host": str(meta.get("host") or meta.get("destinationIP") or ""),
                "dest": str(meta.get("destinationIP") or ""),
                "port": str(meta.get("destinationPort") or ""),
                "net": str(meta.get("network") or ""),
                "type": str(meta.get("type") or ""),
                "proc": str(meta.get("process") or meta.get("processPath") or ""),
                "rule": str(c.get("rule") or ""),
                "chain": str(chains[0]) if chains else "",
                "up": up,
                "down": down,
                "start": str(c.get("start") or ""),
            })
        items.sort(key=lambda x: -(x["down"] + x["up"]))
        return {
            "ok": True,
            "items": items[: max(1, min(int(limit), 1000))],
            "total": len(items),
            "down": _num(raw.get("downloadTotal")),
            "up": _num(raw.get("uploadTotal")),
        }

    def close_connection(self, cid: str = "") -> dict[str, Any]:
        if not cid:
            return {"ok": False, "error": "缺少连接 id"}
        try:
            self.api.close_connection(cid)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def close_all_connections(self) -> dict[str, Any]:
        try:
            self.api.close_all_connections()
            # 全断之后内核要把现有 TCP 都拆掉，流量计数会立刻回落，通知前端刷一次
            self._changed()
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ——— TUN 模式 ———

    def set_tun(self, on: bool) -> dict[str, Any]:
        """TUN 是内核层的虚拟网卡，改完必须重启内核才生效。

        和系统代理不是一回事：系统代理只影响读 WinINET/internet 选项的程序，
        TUN 会在内核里接管整台机器的流量（包括不认代理的命令行工具和游戏）。
        两者不能同时开——同时开会变成「流量进 TUN 又被系统代理再指回 7891」的死循环。
        """
        want = bool(on)
        if want and not netguard.is_admin():
            return {"ok": False, "error": "TUN 需要管理员权限：请退出程序，右键「以管理员身份运行」再开"}

        old = bool(self.settings.get("tun"))
        if old == want:
            return {"ok": True, "unchanged": True, "tun": want}

        self._set(busy="切换 TUN")
        try:
            self.settings["tun"] = want
            self.settings.save()
            # 开 TUN 时把系统代理关掉，避免双重接管
            if want and self.settings.get("system_proxy"):
                self.settings["system_proxy"] = False
                self.settings.save()
                try:
                    sysproxy.disable()
                except Exception:  # noqa: BLE001
                    log.warning("关系统代理失败（继续开 TUN）", exc_info=True)
            ok = self.restart_kernel()
            if not ok:
                self.settings["tun"] = old
                self.settings.save()
                return {"ok": False, "error": "内核重启失败，已回滚", "tun": old}
            self._set(tun=want, tun_on=want)
            return {"ok": True, "tun": want}
        except Exception as e:  # noqa: BLE001
            log.exception("切换 TUN 失败")
            self.settings["tun"] = old
            try:
                self.settings.save()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "tun": old}
        finally:
            self._set(busy="")

    def feedback_send(self, title: str, detail: str, email: str = "") -> dict[str, Any]:
        from . import feedback

        try:
            info = feedback.build(self, title, detail, email)
            self.notify("已生成反馈文件，可在弹出的目录里找到")
            return info
        except Exception as e:  # noqa: BLE001
            log.exception("生成反馈失败")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def feedback_open(self, path: str = "") -> dict[str, Any]:
        from . import feedback

        return {"ok": feedback.open_dir(path)}

    def agree(self, version: str = "") -> dict[str, Any]:
        """记下用户同意的那一版协议。版本变了会重新弹一次。"""
        from . import __version__ as _v

        ver = str(version or _v)
        self.settings["agreed_version"] = ver
        try:
            self.settings.save()
        except Exception as e:  # noqa: BLE001
            log.warning("保存协议同意状态失败: %s", e)
            return {"ok": False, "error": str(e)}
        self._set(agreed=ver)
        log.info("用户已同意使用协议 %s", ver)
        return {"ok": True, "agreed": ver}

    def update_check(self, force: bool = False) -> dict[str, Any]:
        """查 GitHub 最新 release。结果写进 state["update"]，前端轮询就能看到。

        只查不装：装要用户点一下，谁也不想某个后台线程把自己刚调好的东西换掉。
        """
        from . import updater

        repo = str(self.settings.get("update_repo") or "").strip()
        cur = _version()
        if not repo:
            info = {"ok": False, "error": "未配置 update_repo", "current": cur}
        else:
            port = int(self.settings.get("mixed_port", 7891) or 7891)
            try:
                info = updater.check(repo, cur, local_port=port)
            except Exception as e:  # noqa: BLE001
                info = {"ok": False, "error": f"{type(e).__name__}: {e}", "current": cur}
            if info.get("ok") and info.get("has_update") and force:
                log.info("发现新版本 %s（当前 %s）", info.get("latest"), cur)
                self.notify(f"有新版本 {info.get('latest')}，控制台可一键更新")
        with self._lock:
            self.state["update"] = {**info, "checked_at": time.time(), "installing": False}
        self._changed()
        return info

    def update_apply(self) -> dict[str, Any]:
        """下载 → 校验 → 解压 → 交给外部脚本替换，然后自己退出。

        整段在后台跑：安装包 70MB 起步，不能堵住 HTTP 线程。
        """
        if self._update_busy:
            return {"ok": False, "error": "已经在更新中了"}
        with self._lock:
            info = dict(self.state.get("update") or {})
        if not info.get("has_update") or not info.get("zip_url"):
            return {"ok": False, "error": "当前没有可安装的新版本"}
        self._update_busy = True
        self._spawn("update-apply", lambda: self._do_update(info))
        return {"ok": True, "queued": info.get("latest")}

    def _do_update(self, info: dict[str, Any]) -> None:
        from . import updater

        def prog(done: int, total: int) -> None:
            self._set_progress("update", done, total, "下载安装包")

        try:
            self._set(busy="更新中")
            port = int(self.settings.get("mixed_port", 7891) or 7891)
            payload = updater.prepare(info, local_port=port, on_progress=prog)
            self._set_progress("update", 1, 1, "覆盖文件")
            bat = updater.stage(payload, updater.update_workdir())
            updater.launch(bat)
            log.info("更新器已启动，准备退出：%s", bat)
            self._set(update={**info, "installing": True, "checked_at": time.time()})
            time.sleep(1.0)
            # 走正常退出：mihomo 和系统代理才会被清干净，否则新版本一起来就撞端口
            if self.on_quit:
                self.on_quit()
        except updater.UpdateError as e:
            log.warning("更新失败：%s", e)
            self._set(busy="")
            with self._lock:
                self.state["update"] = {**info, "ok": False, "error": str(e),
                                        "checked_at": time.time(), "installing": False}
            self._changed()
            self.notify(f"更新失败：{e}")
        except Exception as e:  # noqa: BLE001
            log.exception("更新异常")
            self._set(busy="")
            with self._lock:
                self.state["update"] = {**info, "ok": False, "error": f"{type(e).__name__}: {e}",
                                        "checked_at": time.time(), "installing": False}
            self._changed()
        finally:
            self._update_busy = False
            self._set_progress()

    def _loop_update(self) -> None:
        """启动 45 秒后查一次（别和启动抢带宽），之后按 update_interval_hours 复查。"""
        if self._stop.wait(45):
            return
        try:
            self.update_check(force=True)
        except Exception as e:  # noqa: BLE001
            log.debug("检查更新异常: %s", e)
        hours = int(self.settings.get("update_interval_hours", 12) or 0)
        if hours <= 0:
            return
        while not self._stop.wait(hours * 3600):
            try:
                self.update_check(force=True)
            except Exception as e:  # noqa: BLE001
                log.debug("检查更新异常: %s", e)
