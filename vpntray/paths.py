"""集中管理运行时路径。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 打包成 exe 后 __file__ 在 _MEIPASS 里，需要区分「代码目录」和「数据目录」
if getattr(sys, "frozen", False):  # PyInstaller
    APP_ROOT = Path(sys.executable).resolve().parent
    _CODE_ROOT = Path(getattr(sys, "_MEIPASS", APP_ROOT))
else:
    APP_ROOT = Path(__file__).resolve().parent.parent
    _CODE_ROOT = APP_ROOT

ROOT = APP_ROOT
BIN = ROOT / "bin"
DATA = ROOT / "data"
LOGS = DATA / "logs"

MIHOMO = BIN / "mihomo.exe"
GEOIP = BIN / "GeoIP.dat"
GEOSITE = BIN / "GeoSite.dat"
MMDB = BIN / "country.mmdb"

CONFIG = DATA / "config.yaml"
SUB_CACHE = DATA / "sub_cache.yaml"
SETTINGS = DATA / "settings.json"
KERNEL_LOG = LOGS / "kernel.log"
APP_LOG = LOGS / "app.log"
UI_DIR = DATA / "ui"
LOCK_FILE = DATA / "app.lock"

# 系统级目录：重置/删除 data 也不会丢，用于保存「接管前的系统代理原始设置」
APPDATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VPNTray"
SYSProxy_BACKUP = APPDATA_DIR / "sysproxy_backup.json"


def ensure_dirs() -> None:
    """建好所有运行时目录。

    只在「目录真的建不出来」时记日志：windowed 进程没有控制台，
    这里直接抛异常等于程序双击后什么都不发生，用户完全无从排查。
    """
    for d in (DATA, LOGS, UI_DIR, APPDATA_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # 不能 import logger（会循环导入），只能走标准 logging
            import logging
            logging.getLogger("paths").warning("创建目录失败 %s: %s", d, e)


def asset(name: str) -> Path:
    """静态资源查找：优先程序目录，其次打包内置（PyInstaller _MEIPASS）。"""
    # frozen 时 _MEIPASS 才是打包进去的那份；ROOT 是 exe 所在目录，
    # 用户可能把它当成「放自己改过的资源」的位置，所以两者都要查，ROOT 优先
    meipass = getattr(sys, "_MEIPASS", None)
    bases = [ROOT / "assets"]
    if meipass and Path(meipass) != ROOT:
        bases.append(Path(meipass) / "assets")
    for base in bases:
        p = base / name
        if p.exists():
            return p
    return ROOT / "assets" / name


def kernel_workdir_args() -> list[str]:
    """mihomo -d 需要在能找到地理数据文件的目录。"""
    return ["-d", str(DATA)]


__all__ = [
    "ROOT", "BIN", "DATA", "LOGS", "MIHOMO", "GEOIP", "GEOSITE", "MMDB",
    "CONFIG", "SUB_CACHE", "SETTINGS", "KERNEL_LOG", "APP_LOG", "UI_DIR",
    "LOCK_FILE", "ensure_dirs", "kernel_workdir_args", "asset",
    "APPDATA_DIR", "SYSProxy_BACKUP",
]
