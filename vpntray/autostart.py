"""开机自启：写 HKCU\\...\\Run，无需管理员。"""
from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

from .paths import ROOT

log = logging.getLogger("autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VPNTray"


def _pythonw() -> Path:
    return Path(sys.executable).with_name("pythonw.exe")


def _command() -> tuple[str, Path]:
    """返回 (写进 Run 的完整命令行, 真正的启动目标)。"""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return f'"{exe}" --minimized', exe
    entry = ROOT / "run.pyw"
    pyw = _pythonw()
    exe = pyw if pyw.exists() else Path(sys.executable)
    return f'"{exe}" "{entry}" --minimized', entry


def enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return bool(v)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def enable() -> None:
    cmd, target = _command()
    # 目标文件不在就别写这一项：写进去只会在下次登录时弹一个
    # 「找不到 C:\...\run.pyw」的错误框，而且用户根本看不出是自启引起的
    if not target.exists():
        raise FileNotFoundError(f"自启目标不存在，已跳过: {target}")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    log.info("已设置开机自启: %s", cmd)


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        log.info("已取消开机自启")
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("取消自启失败: %s", e)


def toggle(on: bool) -> bool:
    if on:
        enable()
    else:
        disable()
    return enabled()


__all__ = ["enabled", "enable", "disable", "toggle"]
