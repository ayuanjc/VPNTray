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
import sys
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


def _config_path() -> Optional[str]:
    """生成的 config.yaml 路径；找不到就给空（nd 会退回 198.18.0.0/15）。"""
    try:
        from . import paths
        path = Path(paths.CONFIG)
    except Exception:          # 打包版路径模块异常不影响诊断
        return None
    return str(path) if path.is_file() else None


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
        # fake-ip 池的真实网段只在生成的 config.yaml 里（mihomo 的 /configs 不含 DNS 段）
        "kernel_config_path": str(_config_path()),
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


def export(app: Any, host: str = "", port: int = 443) -> dict[str, Any]:
    """导出一个可移植诊断包（yuans_nd 1.5.0 起）：host 非空就把详细视图也打进去。"""
    return _guard(app, lambda s: s.export(host=host, port=port))


def caps(app: Any) -> dict[str, Any]:
    return _guard(app, lambda s: s.caps())
def clock(app: Any) -> dict[str, Any]:
    """本机时间与服务器差多少秒（yuans_nd 1.6.0）：证书报错时先排除时钟。"""
    return _guard(app, lambda s: s.clock())


def net(app: Any) -> dict[str, Any]:
    """当前出口与网卡（yuans_nd 1.8.0）：走哪张网卡、v4/v6 是否都有地址。"""
    return _guard(app, lambda s: s.net())


def etw(app: Any, seconds: float = 10.0) -> dict[str, Any]:
    """按需抓一段 DNS/TCPIP 事件（yuans_nd 1.6.0）：需要管理员，非管理员只回命令模板。"""
    out = _guard(app, lambda s: s.etw(seconds=seconds, top=10))
    data = out.get('data') if isinstance(out.get('data'), dict) else out
    if isinstance(data, dict) and data.get('ok') is False:
        err = data.get('error') or {}
        if err.get('code') == 'ERR-ACT-NEED-ADMIN':
            out['elevate'] = etw_cmd(app, seconds)
    return out


def etw_cmd(app: Any, seconds: float = 10.0) -> dict[str, Any]:
    """拼一条 nd etw 的提权命令：宿主不自己提权，只给用户可点的东西。"""
    ok, err = available()
    if not ok:
        return {"available": False, "error": err}
    try:
        from yuans_nd import elevate as elev

        tail = ["etw", "--seconds", str(round(float(seconds), 1)), "--top", "10"]
        hint = dict(elev.build_cli(tail, data_dir=_data_dir(app)))
        if getattr(sys, "frozen", False):
            # 打包版没有独立 python：sys.executable 是 VPNTray.exe，-m 跑不起来
            hint["available"] = False
            hint["display"] = ""
            hint["msg"] = "打包版没有独立 python：右键托盘退出，用管理员身份重开 VPNTray 后再点这个按钮"
        return hint
    except Exception as exc:
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}


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
