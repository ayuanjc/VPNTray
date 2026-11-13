"""AI 接口：把本程序的能力暴露成一小组「工具」，让本机 AI 客户端直接操作。

为什么是「清单 + 一个调用端点」而不是一整套 REST
-------------------------------------------------
AI 客户端要的不是资源语义，而是「有哪些动作 / 每个动作吃什么参数 / 调完返回
什么」。所以只有两个端点，任何 AI 都能在第一次 /ai/v1/manifest 之后自解释：

    GET  /ai/v1/manifest          工具清单（名字、参数、级别、示例）
    POST /ai/v1/call               {"tool": "...", "args": {...}}

安全边界（三层，每层都独立生效）
--------------------------------
* 网络：webui 只绑 127.0.0.1 且校验 Host；这里再校验对端是回环。
* 凭证：settings["ai_api_token"]，首次使用时生成，面板里能看到/重置。
  请求带 X-AI-Token 或 Authorization: Bearer <token>，也可以用 ?token=。
* 权限：read < write < full。settings["ai_api_level"] 是上限（默认 full：
  用户装这个软件就是为了让 AI 顺手把配置改掉）。
* 参数：每个工具自己收参数，统一走 _s/_b/_i 收敛，从不把 args 原样 splat。

写操作都落在和数据文件/状态机同一套实现上（app.module_save 等），所以
「AI 改的」和「用户在面板上改的」完全等价，不会出现两份真相。
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from typing import Any, Callable

from . import __version__ as _VERSION
from . import modules as modules_mod

log = logging.getLogger("aiops")

LEVELS = {"read": 0, "write": 1, "full": 2}
LEVEL_TEXT = {"read": "只读", "write": "可改配置", "full": "完全控制"}
TOKEN_KEY = "ai_api_token"
ENABLED_KEY = "ai_api_enabled"
LEVEL_KEY = "ai_api_level"


# ────────────────────────── 参数收敛 ──────────────────────────
def _s(args: dict, key: str, default: str = "", cap: int = 200) -> str:
    v = args.get(key, default)
    return str(v if v is not None else "").strip()[:cap]


def _b(args: dict, key: str, default: bool = False) -> bool:
    v = args.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _i(args: dict, key: str, default: int = 0, lo: int = -1 << 30, hi: int = 1 << 30) -> int:
    try:
        n = int(args.get(key, default))
    except (TypeError, ValueError):
        return default
    return n if lo <= n <= hi else default


def _names(args: dict, key: str = "names", cap: int = 20) -> list[str]:
    v = args.get(key)
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v if isinstance(v, (list, tuple)) else []:
        s = str(x).strip()
        if s:
            out.append(s[:120])
        if len(out) >= cap:
            break
    return out


# ────────────────────────── 工具表 ──────────────────────────
TOOLS: dict[str, dict[str, Any]] = {}


def _reg(name: str, level: str, title: str, desc: str,
         params: dict[str, str], fn: Callable[[Any, dict], Any],
         example: dict[str, Any] | None = None, long: bool = False) -> None:
    TOOLS[name] = {
        "name": name,
        "level": level,
        "title": title,
        "desc": desc,
        "params": params,          # {"参数名": "类型/说明"}
        "example": example or {},
        "long": long,              # 耗时长，返回 queued 而不是结果
    }
    _HANDLERS[name] = fn


_HANDLERS: dict[str, Callable[[Any, dict], Any]] = {}


# ── 只读 ──
def _t_status(app, args):
    snap = app.snapshot()
    st = app.state or {}
    groups = (st.get("proxies") or {})
    main = str(st.get("group_now") or "")
    return {
        "ok": True,
        "version": _VERSION,
        "phase": snap.get("phase"),
        "kernel_ok": bool(snap.get("kernel_ok")),
        "mode": snap.get("mode"),
        "current": snap.get("current"),
        "main_group": app.settings.get("main_group"),
        "main_now": main,
        "groups": [g for g, v in groups.items() if (v or {}).get("type") == "Selector"][:40],
        "nodes": len([1 for v in groups.values() if (v or {}).get("type") not in
                      ("Selector", "Fallback", "URLTest", "LoadBalance", "Direct", "Reject")]),
        "sysproxy": bool(snap.get("sysproxy")),
        "dns_running": bool(st.get("dns_running")),
        "busy": snap.get("busy"),
        "error": snap.get("error", ""),
    }


def _t_modules_list(app, args):
    return {"ok": True, "modules": app.modules_status()}


def _t_modules_get(app, args):
    key = _s(args, "key", cap=32).lower()
    mod = app.module_get(key)
    if not mod:
        return {"ok": False, "error": f"未知模块：{key}"}
    return {"ok": True, "module": mod}


def _t_nodes(app, args):
    st = app.state or {}
    delays = st.get("delays") or {}
    out = []
    for name, g in (st.get("proxies") or {}).items():
        if (g or {}).get("type") in ("Selector", "Fallback", "URLTest", "LoadBalance",
                                     "Direct", "Reject", "Compatible", "Pass"):
            continue
        out.append({"name": name, "delay": delays.get(name)})
    out.sort(key=lambda x: (x["delay"] is None, x["delay"] or 0))
    return {"ok": True, "nodes": out[:400], "total": len(out)}


def _t_ai_status(app, args):
    return {"ok": True, "ai": app.ai_status()}


def _t_subs(app, args):
    return {"ok": True, "subs": app.subscriptions(), "active": app.active_sub(),
            "mode": app.sub_mode(), "pool": len(app.sub_pool())}


def _t_log(app, args):
    from .paths import APP_LOG
    n = _i(args, "lines", 60, 1, 400)
    try:
        lines = APP_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError as e:
        return {"ok": False, "error": f"读日志失败：{e}"}
    return {"ok": True, "lines": lines}


# ── 写：模块 ──
def _t_module_save(app, args):
    mod = args.get("module") if isinstance(args.get("module"), dict) else args
    ok, err = app.module_save(mod)
    return {"ok": ok, "error": err, "modules": app.modules_status()}


def _t_module_delete(app, args):
    ok, err = app.module_delete(_s(args, "key", cap=32))
    return {"ok": ok, "error": err, "modules": app.modules_status()}


def _t_module_toggle(app, args):
    key = _s(args, "key", cap=32)
    sc = app.module_get(key)
    if not sc:
        return {"ok": False, "error": f"未知模块：{key}"}
    ok, err = app.set_scene_enabled(key, _b(args, "on", True))
    return {"ok": ok, "error": err, "enabled": _b(args, "on", True)}


def _t_module_reset(app, args):
    ok, err = app.module_reset()
    return {"ok": ok, "error": err, "modules": app.modules_status()}


def _t_module_import(app, args):
    payload = args.get("modules") if "modules" in args else args.get("json")
    ok, err = app.module_import(payload)
    return {"ok": ok, "error": "" if ok else err, "note": err if ok else "",
            "modules": app.modules_status()}


def _t_module_export(app, args):
    return {"ok": True, "export": app.module_export()}


# ── 写：出口 / 订阅 ──
def _t_scene_select(app, args):
    ok, err = app.set_scene_node(_s(args, "key", cap=32), _s(args, "name", cap=120))
    return {"ok": ok, "error": err}


def _t_ai_select(app, args):
    ok, err = app.set_ai_node(_s(args, "name", cap=120))
    return {"ok": ok, "error": err}


def _t_ai_toggle(app, args):
    ok, err = app.set_ai_enabled(_b(args, "on", True))
    return {"ok": ok, "error": err}


def _t_node_select(app, args):
    name = _s(args, "name", cap=120)
    if not name:
        return {"ok": False, "error": "缺少 name"}
    return {"ok": bool(app.select_node(name)), "current": (app.snapshot() or {}).get("current")}


def _t_mode(app, args):
    mode = _s(args, "mode", "rule", 16).lower()
    if mode not in ("rule", "global", "direct"):
        return {"ok": False, "error": "mode 只能是 rule / global / direct"}
    app.set_mode(mode)
    return {"ok": True, "mode": mode}


def _t_sub_add(app, args):
    ok, err = app.set_subscription(_s(args, "url", cap=2000), _s(args, "name", cap=60))
    return {"ok": ok, "error": err}


def _t_sub_use(app, args):
    ok, err = app.use_subscription(_i(args, "index", 0, 0, 99), _b(args, "keep_node", False))
    return {"ok": ok, "error": err}


def _t_sub_remove(app, args):
    ok, err = app.remove_subscription(_i(args, "index", 0, 0, 99))
    return {"ok": ok, "error": err}


def _t_sub_update(app, args):
    ok = app.update_subscription(force=_b(args, "force", True))
    return {"ok": bool(ok), "error": "" if ok else "更新失败，看 /api/state 的 sub.error"}


# ── 完全控制 ──
def _t_kernel_restart(app, args):
    app.restart_kernel()
    return {"ok": True, "queued": "restart"}


def _t_proxy(app, args):
    on = _b(args, "on", True)
    app.toggle_system_proxy(on)
    return {"ok": True, "on": on}


def _t_autostart(app, args):
    on = _b(args, "on", True)
    app.set_autostart(on)
    return {"ok": True, "on": on}


def _t_tun(app, args):
    return app.set_tun(_b(args, "on", True))


def _t_speed(app, args):
    names = _names(args, "names", 20)
    limit = _i(args, "limit", 10, 1, 60)
    threading.Thread(target=app.check_speed, args=(names or None, limit), daemon=True).start()
    return {"ok": True, "queued": "speed"}


def _t_purity(app, args):
    names = _names(args, "names", 20)
    threading.Thread(target=app.check_purity, args=(names or None, 0), daemon=True).start()
    return {"ok": True, "queued": "purity"}


def _t_power(app, args):
    return app.power(_b(args, "on", True))


def _t_update_check(app, args):
    return app.update_check(force=_b(args, "force", True))


def _t_update_apply(app, args):
    return app.update_apply()


def _t_quit(app, args):
    threading.Timer(0.6, lambda: getattr(app, "on_quit", lambda: None)()).start()
    return {"ok": True, "note": "程序将在 0.6 秒后退出（代理会中断）"}


_reg("status", "read", "运行状态", "版本、阶段、当前节点、主组、系统代理由谁拿着", {}, _t_status)
_reg("modules.list", "read", "模块清单", "所有场景/规则模块（不含完整域名表）", {}, _t_modules_list)
_reg("modules.get", "read", "单个模块", "读出完整 JSON（域名、规则、组参数）",
     {"key": "模块 key，如 github"}, _t_modules_get, {"key": "github"})
_reg("nodes.list", "read", "节点清单", "按延迟升序的节点名", {}, _t_nodes)
_reg("ai.status", "read", "AI 通道", "AI 专用组的当前出口与出口国家", {}, _t_ai_status)
_reg("subs.list", "read", "订阅清单", "所有订阅、当前订阅、合并池大小", {}, _t_subs)
_reg("log.tail", "read", "最近日志", "app.log 末尾若干行",
     {"lines": "1–400，默认 60"}, _t_log, {"lines": 80})

_reg("modules.save", "write", "保存模块", "新建或覆盖一个模块（改完立即热重载）",
     {"module": "完整模块对象；只改一个字段就先 modules.get 再整体回传"},
     _t_module_save,
     {"module": {"key": "epic", "title": "Epic 加速", "kind": "scene",
                 "match": {"domains": ["epicgames.com", "unrealengine.com"]}}})
_reg("modules.delete", "write", "删除模块", "删掉 data/modules/<key>.json 并重载",
     {"key": "模块 key"}, _t_module_delete, {"key": "epic"})
_reg("modules.toggle", "write", "开关模块", "启用/关闭（关闭＝不建组也不插规则）",
     {"key": "模块 key", "on": "true / false"}, _t_module_toggle, {"key": "steam", "on": False})
_reg("modules.import", "write", "批量导入", "导入 {modules:[...]} 或模块数组",
     {"modules": "模块数组或 {modules:[...]}"}, _t_module_import)
_reg("modules.export", "read", "导出模块", "导出全部模块（备份用）", {}, _t_module_export)
_reg("modules.reset", "write", "恢复内置", "把 Steam / GitHub 写回出厂值（自建模块不动）",
     {}, _t_module_reset)
_reg("scene.select", "write", "钉模块出口", "把某个场景模块的出口钉到指定节点",
     {"key": "模块 key", "name": "节点名"}, _t_scene_select, {"key": "github", "name": "香港-IEPL 01"})
_reg("ai.select", "write", "钉 AI 出口", "把 AI 组钉到指定节点",
     {"name": "节点名"}, _t_ai_select)
_reg("ai.toggle", "write", "开关 AI 通道", "AI 域名是否走独立组",
     {"on": "true / false"}, _t_ai_toggle)
_reg("node.select", "write", "切换主组节点", "主策略组换一个出口",
     {"name": "节点名"}, _t_node_select)
_reg("mode.set", "write", "运行模式", "rule / global / direct",
     {"mode": "rule|global|direct"}, _t_mode, {"mode": "rule"})
_reg("sub.add", "write", "添加订阅", "加一条订阅并切换过去",
     {"url": "订阅链接", "name": "备注名"}, _t_sub_add)
_reg("sub.use", "write", "切换订阅", "按下标启用某条订阅",
     {"index": "0 起", "keep_node": "true=保持当前节点"}, _t_sub_use, {"index": 0})
_reg("sub.remove", "write", "删除订阅", "按下标删除订阅",
     {"index": "0 起"}, _t_sub_remove, {"index": 1})
_reg("sub.update", "write", "更新订阅", "立即拉一次订阅并重建配置",
     {"force": "true=绕过缓存"}, _t_sub_update, {"force": True})

_reg("kernel.restart", "full", "重启内核", "重启 mihomo（连接会断一下）", {}, _t_kernel_restart)
_reg("proxy.system", "full", "系统代理", "接管 / 释放 Windows 系统代理",
     {"on": "true / false"}, _t_proxy)
_reg("autostart.set", "full", "开机自启", "写注册表 Run 项",
     {"on": "true / false"}, _t_autostart)
_reg("tun.set", "full", "TUN 模式", "开/关 TUN（会短暂断网）",
     {"on": "true / false"}, _t_tun)
_reg("test.speed", "full", "测带宽", "后台排队测速，结果看 /api/state 的 speed",
     {"names": "节点名数组，空=自动挑", "limit": "最多测几个"}, _t_speed)
_reg("test.purity", "full", "测纯净度", "后台排队查出口 IP / 纯净度",
     {"names": "节点名数组"}, _t_purity)
_reg("power", "full", "总开关", "连接 / 断开（不退出进程）",
     {"on": "true / false"}, _t_power)
_reg("update.check", "full", "检查更新", "查 GitHub Release",
     {"force": "true=忽略缓存"}, _t_update_check)
_reg("update.apply", "full", "安装更新", "下载并安装新版本（程序会重启）", {}, _t_update_apply)
_reg("app.quit", "full", "退出程序", "关闭 VPNTray（系统代理会释放）", {}, _t_quit)


# ────────────────────────── 凭证 / 权限 ──────────────────────────
def ensure_token(settings) -> str:
    tok = str(settings.get(TOKEN_KEY) or "")
    if len(tok) < 16:
        tok = secrets.token_urlsafe(24)
        settings[TOKEN_KEY] = tok
        try:
            settings.save()
        except Exception as e:  # noqa: BLE001
            log.warning("保存 AI token 失败：%s", e)
    return tok


def level(settings) -> str:
    lv = str(settings.get(LEVEL_KEY) or "full").lower()
    return lv if lv in LEVELS else "full"


def enabled(settings) -> bool:
    return bool(settings.get(ENABLED_KEY, True))


def state(app) -> dict[str, Any]:
    s = app.settings
    tok = ensure_token(s)
    return {
        "enabled": enabled(s),
        "level": level(s),
        "level_text": LEVEL_TEXT.get(level(s), ""),
        "token": tok,
        "path": "/ai/v1/call",
        "manifest": "/ai/v1/manifest",
        "tools": len(TOOLS),
        "hint": "GET /ai/v1/manifest 看清单；POST /ai/v1/call 执行；头部带 X-AI-Token",
    }


def auth(app, headers: Any, query_token: str = "") -> tuple[bool, str]:
    """token 校验。headers 是 email.message 那种 get() 可用的对象或 dict。"""
    s = app.settings
    if not enabled(s):
        return False, "AI 接口已关闭（设置里打开，或 POST /api/ai/api {\"enabled\":true}）"

    def h(name: str) -> str:
        try:
            return str(headers.get(name) or "")
        except Exception:  # noqa: BLE001
            return ""

    tok = ensure_token(s)
    given = h("X-AI-Token").strip()
    if not given:
        a = h("Authorization").strip()
        if a.lower().startswith("bearer "):
            given = a[7:].strip()
    if not given:
        given = (query_token or "").strip()
    if not given or not hmac.compare_digest(given, tok):
        return False, "token 不对：在面板「AI 接口」卡片里复制，或用 /api/ai/api 重置"
    return True, ""


def manifest(app) -> dict[str, Any]:
    lv = LEVELS[level(app.settings)]
    tools = []
    for t in TOOLS.values():
        tools.append({
            "name": t["name"],
            "title": t["title"],
            "desc": t["desc"],
            "level": t["level"],
            "allowed": LEVELS[t["level"]] <= lv,
            "params": t["params"],
            "example": t["example"],
            "long": t["long"],
        })
    return {
        "ok": True,
        "api": "vpn-tray-ai",
        "version": _VERSION,
        "level": level(app.settings),
        "call": "/ai/v1/call",
        "auth": "X-AI-Token: <token>  或  Authorization: Bearer <token>",
        "tools": tools,
    }


def call(app, tool: str, args: Any) -> dict[str, Any]:
    name = str(tool or "").strip()
    t = TOOLS.get(name)
    if not t:
        close = [k for k in TOOLS if name and (name in k or k in name)][:5]
        return {"ok": False, "error": f"没有这个工具：{name}",
                "hint": close or ["先 GET /ai/v1/manifest"]}
    cap = LEVELS[level(app.settings)]
    if LEVELS[t["level"]] > cap:
        return {"ok": False, "error": f"「{t['title']}」需要 {t['level']} 权限，当前上限是 {level(app.settings)}",
                "need": t["level"]}
    a = args if isinstance(args, dict) else {}
    fn = _HANDLERS[name]
    try:
        res = fn(app, a)
    except Exception as e:  # noqa: BLE001
        log.exception("AI 调用 %s 失败", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(res, dict):
        res = {"ok": True, "result": res}
    res.setdefault("ok", True)
    log.info("AI 调用 %s（%s）→ ok=%s", name, t["level"], res.get("ok"))
    return res


def dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode()
