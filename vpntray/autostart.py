"""开机自启：写 HKCU\\...\\Run，无需管理员。"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows：整个模块退化成「不支持自启」，不要连导入都炸
    winreg = None  # type: ignore[assignment]

from .paths import ROOT

log = logging.getLogger("autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VPNTray"


def _pythonw() -> Path:
    """优先返回 pythonw.exe：用 python.exe 起托盘，每次开机都会闪一个黑框。"""
    exe = Path(sys.executable)
    for cand in (exe.with_name("pythonw.exe"),
                 Path(sys.prefix) / "pythonw.exe",
                 Path(getattr(sys, "base_prefix", sys.prefix)) / "pythonw.exe"):
        try:
            if cand.exists():
                return cand
        except OSError:
            continue
    # 实在没有 pythonw 才退回 python.exe，并留下线索（自启黑框是用户能报上来的问题）
    log.warning("找不到 pythonw.exe，自启时会短暂出现控制台窗口")
    return exe


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
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return bool(v)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _write_value(cmd: str) -> bool:
    """写 Run 项并回读校验。返回 False 表示这次写入没有真正落地。"""
    if winreg is None:
        return False
    try:
        # CreateKeyEx：Run 键被安全软件/组策略删掉时 OpenKey 会直接 FileNotFoundError，
        # 而 CreateKeyEx 会把它建回来（HKCU 下不需要管理员权限）。
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
            winreg.KEY_SET_VALUE | winreg.KEY_READ,
        ) as k:
            winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
            # 回读校验：安全软件会静默拦掉这类写入，只看 SetValueEx 不抛异常会误报成功
            got, _ = winreg.QueryValueEx(k, VALUE_NAME)
    except OSError as e:
        log.warning("写自启项失败: %s", e)
        return False
    if str(got).strip() != cmd:
        log.warning("自启项回读不一致: %r != %r", got, cmd)
        return False
    return True


def enable() -> None:
    cmd, target = _command()
    # 目标文件不在就别写这一项：写进去只会在下次登录时弹一个
    # 「找不到 C:\...\run.pyw」的错误框，而且用户根本看不出是自启引起的
    if not target.exists():
        raise FileNotFoundError(f"自启目标不存在，已跳过: {target}")
    if not _write_value(cmd):
        raise OSError("写入开机自启项失败（可能被安全软件拦截）")
    log.info("已设置开机自启: %s", cmd)


def disable() -> None:
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        log.info("已取消开机自启")
    except FileNotFoundError:
        pass
    except OSError as e:
        log.warning("取消自启失败: %s", e)


def refresh() -> bool:
    """自启项里写的还是别的目录的 exe 时，改写成本进程的。

    升级最容易踩的坑：新版解压到新目录，注册表里还是旧目录那个 exe，
    下次开机启动的仍然是旧版本 —— 用户看到的就是「我明明更新了，界面没变」。
    只修正「已经是开」的情况，绝不替用户打开自启。
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            cur, _ = winreg.QueryValueEx(k, VALUE_NAME)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    cmd, target = _command()
    if str(cur).strip() == cmd or not target.exists():
        return False
    if not _write_value(cmd):
        log.warning("修正自启路径失败（写入未落地）")
        return False
    log.info("自启路径已更新: %s → %s", cur, cmd)
    return True


def toggle(on: bool) -> bool:
    if on:
        enable()
    else:
        disable()
    return enabled()


__all__ = ["enabled", "enable", "disable", "toggle"]
