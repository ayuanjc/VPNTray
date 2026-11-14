"""诊断内核宿主适配（yuans_nd 1.4.x）。

依赖方向：VPNTray -> yuans_nd，反向禁止。yuans_nd 不认识我们的 App，
只拿到一个 _Host，里面实现 restart_kernel()；它用 getattr 探测缺什么，
缺了给明确原因，不会崩。

两个刻意的选择：
1) 懒启动。诊断内核一起就每 2 秒轮询内核 /connections，没人看诊断页时
   白烧 CPU。第一次查状态才 start()。
2) 密钥只在内存里。kernel_secret_ref 传字面值（mihomo 的 secret 本来就在
   我们进程内存里），并且从不调用 nd 的 configure()，所以它不会落进
   nd 的 config.json。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
_SVC: Any = None
_ERR = ""

# 诊断页不让人填这些：宿主知道的一律由宿主喂
_POLL_S = 2


def version() -> str:
    try:
        import yuans_nd

        return str(getattr(yuans_nd, "__version__", "") or "")
    except Exception:
        return ""


def available() -> tuple[bool, str]:
    try:
        import yuans_nd  # noqa: F401
    except Exception as exc:  # 打包漏了、路径没带
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


class _Host:
    """交给 yuans_nd 的宿主句柄。只有它真的需要的能力才实现。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    def restart_kernel(self) -> bool:
        try:
            return bool(self.app.restart_kernel())
        except Exception as exc:
            log.warning("诊断内核请求重启内核失败：%s", exc)
            return False


def _data_dir(app: Any) -> str:
    root = str(app.settings.get("data_dir") or "").strip()
    base = Path(root) if root else Path.home() / ".vpntray"
    return str(base / "doctor")


def _values(app: Any) -> dict[str, Any]:
    st = app.settings
    secret = str(st.get("secret") or "").strip()
    return {
        "kernel_host": "127.0.0.1",
        "kernel_port": int(st.get("controller_port") or 9091),
        # 字面值：Host 没有 secret() 接口，env: 又要污染子进程环境
        "kernel_secret_ref": secret or "host",
        "mixed_port": int(st.get("mixed_port") or 7891),
        "interval_s": _POLL_S,
        # 用户点名的目标优先于「今天恰好连过的域名」，见 nd 的 service._targets
        "probe_targets": [str(x).strip() for x in (st.get("doctor_targets") or []) if str(x).strip()][:8],
        "action_enabled": bool(st.get("doctor_actions", True)),
        "allow_dangerous_actions": False,
        # 宿主进程内调用，别再开一个 HTTP 端口
        "api_enabled": False,
        "log_level": "warning",
    }


def _svc(app: Any) -> Any:
    global _SVC
    with _LOCK:
        if _SVC is not None:
            return _SVC
        from yuans_nd.service import Service

        svc = Service(data_dir=_data_dir(app), values=_values(app))
        svc.set_host(_Host(app))
        svc.start()
        _SVC = svc
        return svc


def _guard(app: Any, fn: Any, *args: Any) -> dict[str, Any]:
    global _ERR
    ok, why = available()
    if not ok:
        _ERR = why
        return {"ok": False, "error": {"code": "ERR-ND-MISSING", "msg": why}}
    try:
        out = fn(_svc(app), *args)
        _ERR = ""
        return out if isinstance(out, dict) else {"ok": True, "data": out}
    except Exception as exc:  # 宿主边界：诊断挂了不能拖死主程序
        _ERR = f"{type(exc).__name__}: {exc}"
        log.warning("诊断内核调用失败：%s", _ERR)
        return {"ok": False, "error": {"code": "ERR-ND-INTERNAL", "msg": _ERR}}


def light(app: Any) -> dict[str, Any]:
    """给 /api/state 的轻量快照：不启动采集，只报「能不能用、起了没」。"""
    ok, err = available()
    with _LOCK:
        started = _SVC is not None
    return {
        "enabled": bool(app.settings.get("doctor", True)),
        "available": ok,
        "version": version(),
        "started": started,
        "error": err,
    }


def start(app: Any) -> dict[str, Any]:
    """显式预热（可选）。正常路径是第一次查状态时懒启动。"""
    return _guard(app, lambda s: s.status())


def stop() -> None:
    global _SVC
    with _LOCK:
        svc, _SVC = _SVC, None
    if svc is not None:
        try:
            svc.stop()
        except Exception as exc:
            log.warning("诊断内核停止失败：%s", exc)


def status(app: Any) -> dict[str, Any]:
    out = _guard(app, lambda s: s.status())
    out["host_version"] = version()
    out["host_ok"], out["host_error"] = available()
    out["data_dir"] = _data_dir(app)
    return out


def diagnose(app: Any, mode: str = "quick", text: str = "") -> dict[str, Any]:
    return _guard(app, lambda s: s.diagnose(mode=mode, text=text))


def detail(app: Any, host: str, port: int = 443) -> dict[str, Any]:
    return _guard(app, lambda s: s.detail(host, port=port))


def events(app: Any, limit: int = 80, kind: str = "") -> dict[str, Any]:
    return _guard(app, lambda s: s.events(limit=limit, kind=kind))


def caps(app: Any) -> dict[str, Any]:
    return _guard(app, lambda s: s.caps())


def actions(app: Any) -> dict[str, Any]:
    return _guard(app, lambda s: s.actions())


def packs(app: Any) -> dict[str, Any]:
    return _guard(app, lambda s: s.packs())


def fix(app: Any, act_id: str, why: str = "", params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return _guard(app, lambda s: s.fix(act_id, params or {}, by="user", why=why))


def undo(app: Any, run_id: str) -> dict[str, Any]:
    return _guard(app, lambda s: s.undo(run_id, by="user"))


def elevate(app: Any, act_id: str, why: str = "") -> dict[str, Any]:
    """拼好一条提权命令并交给用户点（弹 UAC）；服务端自己不提权。"""
    ok, err = available()
    if not ok:
        return {"ok": False, "error": {"code": "ERR-ND-MISSING", "msg": err}}
    try:
        from yuans_nd import elevate as elev

        hint = elev.build(act_id, why=why, data_dir=_data_dir(app))
        return {"ok": True, **hint}
    except Exception as exc:
        return {"ok": False, "error": {"code": "ERR-ND-INTERNAL", "msg": f"{type(exc).__name__}: {exc}"}}


def elevate_run(app: Any, act_id: str, why: str = "") -> dict[str, Any]:
    ok, err = available()
    if not ok:
        return {"ok": False, "error": {"code": "ERR-ND-MISSING", "msg": err}}
    try:
        from yuans_nd import elevate as elev

        hint = elev.build(act_id, why=why, data_dir=_data_dir(app))
        res = elev.run(hint)
        return {"ok": bool(res.get("ok")), "elevate": hint, "result": res}
    except Exception as exc:
        return {"ok": False, "error": {"code": "ERR-ND-INTERNAL", "msg": f"{type(exc).__name__}: {exc}"}}
