"""Windows 系统代理开关（注册表 + WinINet 即时生效），带原值备份与还原。"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
import winreg
from ctypes import wintypes

from .paths import DATA, SYSProxy_BACKUP, ensure_dirs

_lock = threading.RLock()

log = logging.getLogger("sysproxy")

KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# 备份放在 %LOCALAPPDATA%\VPNTray\，这样重置/删除 data 目录也不会丢，
# 否则「原始系统代理设置」一丢，退出时就会把用户原先的代理清掉。
BACKUP = SYSProxy_BACKUP
LEGACY_BACKUP = DATA / "sysproxy_backup.json"

INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39

BYPASS = (
    "localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.2*;"
    "172.30.*;172.31.*;192.168.*;<local>"
)


def _internet_set_option() -> None:
    try:
        wininet = ctypes.WinDLL("wininet.dll")
        wininet.InternetSetOptionW(None, INTERNET_OPTION_SETTINGS_CHANGED, None, 0)
        wininet.InternetSetOptionW(None, INTERNET_OPTION_REFRESH, None, 0)
    except Exception as e:  # noqa: BLE001
        log.debug("刷新系统代理设置失败: %s", e)


def _read(name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return v
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _write(name: str, value, kind=winreg.REG_SZ) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, kind, value)


def _delete(name: str) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except OSError:
        pass


def snapshot() -> dict:
    return {
        "ProxyEnable": _read("ProxyEnable"),
        "ProxyServer": _read("ProxyServer"),
        "ProxyOverride": _read("ProxyOverride"),
        "AutoConfigURL": _read("AutoConfigURL"),
    }


def _load_backup() -> dict | None:
    """优先读系统级备份；只有旧位置有的话顺手迁过去。"""
    for p in (BACKUP, LEGACY_BACKUP):
        try:
            if not p.exists():
                continue
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d:
                if p == LEGACY_BACKUP and not BACKUP.exists():
                    log.info("迁移系统代理备份到 %s", BACKUP)
                    _save_backup(d)
                return d
        except Exception:  # noqa: BLE001
            continue
    return None


def _save_backup(d: dict) -> None:
    """原子写 + 双份：备份是「退出时还原」的唯一依据，写坏了就是永久断网。"""
    ensure_dirs()
    body = json.dumps(d, ensure_ascii=False, indent=2)
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    tmp = BACKUP.with_name(BACKUP.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, BACKUP)
    except OSError as e:
        log.warning("写入系统代理备份失败: %s", e)
    # 旧位置也写一份，方便回退到旧版本时不丢
    try:
        LEGACY_BACKUP.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_BACKUP.write_text(body, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def current() -> dict:
    return snapshot()


def is_enabled_to(server: str) -> bool:
    """系统代理是否正指向 server。

    ProxyServer 有两种写法：裸 `host:port`，或按协议分开的
    `http=h:p;https=h:p;socks=h:p`。只比裸串会把后者判成「没开」，
    于是健康检查一路 enable，把系统代理反复往回拽。
    """
    if not _read("ProxyEnable"):
        return False
    raw = str(_read("ProxyServer") or "").strip()
    if not raw:
        return False
    if raw == server:
        return True
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        host = part.split("=", 1)[-1].strip()
        if host == server:
            return True
    return False


def enable(server: str = "127.0.0.1:7891") -> None:
    """接管系统代理；首次接管时备份原设置。

    关键防御：如果备份内容本身就是我们的端口（上次异常退出留下的残留、
    或两次 enable 竞争导致备份到了错误的值），绝不能把它当还原目标，
    否则退出时会把系统代理「还原」成一个没人监听的端口，直接断网。
    """
    # 加锁：健康看护线程每 20 秒会 enable 一次，和用户点开关并发时
    # 两边都可能看到「没有备份」，后写的一方会把用户真实原设置永久覆盖掉。
    with _lock:
        bak = _load_backup()
        if bak is None or (bak.get("ProxyServer") or "") == server:
            fresh = snapshot()
            if (fresh.get("ProxyServer") or "") == server:
                log.info("检测到上次残留的代理设置，还原目标改为「关闭系统代理」")
                fresh = {"ProxyEnable": 0, "ProxyServer": None,
                         "ProxyOverride": None, "AutoConfigURL": None}
            _save_backup(fresh)
            log.info("已备份原系统代理设置: %s", fresh.get("ProxyServer"))

        if _read("AutoConfigURL"):
            _delete("AutoConfigURL")
        _write("ProxyEnable", 1, winreg.REG_DWORD)
        _write("ProxyServer", server)
        _write("ProxyOverride", BYPASS)
        _internet_set_option()
    log.info("系统代理已开启 -> %s", server)


def _safe(fn, what: str) -> int:
    """执行一步注册表操作，吞掉异常只记日志。返回 1 表示失败。"""
    try:
        fn()
        return 0
    except Exception as e:  # noqa: BLE001
        log.warning("%s 失败: %s", what, e)
        return 1


def disable() -> None:
    """还原为接管前的设置。

    这里每一步都必须独立兜底：只要有一项抛异常就整体中断，
    可能停在「ProxyEnable=1 但 ProxyServer 已被删」的状态 —— 那就是全系统断网。
    """
    bak = _load_backup() or {}
    failed = 0
    if bak:
        if bak.get("ProxyEnable") is None:
            failed += _safe(lambda: _delete("ProxyEnable"), "删除 ProxyEnable")
        else:
            failed += _safe(lambda: _write("ProxyEnable", int(bak["ProxyEnable"] or 0),
                                          winreg.REG_DWORD), "还原 ProxyEnable")
        for name in ("ProxyServer", "ProxyOverride", "AutoConfigURL"):
            v = bak.get(name)
            if v:
                failed += _safe(lambda n=name, val=v: _write(n, val), f"还原 {name}")
            else:
                failed += _safe(lambda n=name: _delete(n), f"删除 {name}")
        if failed:
            log.warning("系统代理还原有 %d 项失败，强制关闭代理以免断网", failed)
            _safe(lambda: _write("ProxyEnable", 0, winreg.REG_DWORD), "强制关闭 ProxyEnable")
            _safe(lambda: _delete("ProxyServer"), "强制删除 ProxyServer")
        else:
            log.info("系统代理已还原 -> %s", _read("ProxyServer") or "（直连）")
    else:
        # 连备份都没有，只能保证「关闭代理」这种可用状态，并留下明显日志
        log.warning("未找到系统代理备份，已关闭系统代理（直连）")
        _safe(lambda: _write("ProxyEnable", 0, winreg.REG_DWORD), "关闭 ProxyEnable")
        _safe(lambda: _delete("ProxyServer"), "删除 ProxyServer")
    _internet_set_option()


__all__ = ["enable", "disable", "snapshot", "current", "is_enabled_to", "BYPASS"]
