"""集中管理运行时路径。"""
from __future__ import annotations

import os
import sys
import tempfile
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

# 系统级目录：重置/删除 data 也不会丢，用于保存「接管前的系统代理原始设置」；
# 同时它也是 data 目录不可写时的落点（%LOCALAPPDATA% 一定属于当前用户）。
APPDATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "VPNTray"
SYSProxy_BACKUP = APPDATA_DIR / "sysproxy_backup.json"

# 正常落点：frozen 时就是 exe 所在目录下的 data/（跟着程序走，绿色版习惯）
_PRIMARY_DATA = ROOT / "data"


def _probe_writable(directory: Path) -> tuple[bool, str]:
    """用临时文件真写一次来判断目录可写，而不是看 os.access。

    os.access 在 Windows 上反映的是 ACL 的「有效权限」，再叠加 UAC 的文件虚拟化，
    装在 Program Files 时它会说目录可写，真写才抛 PermissionError —— 用户看到的是
    「界面一切正常，订阅/日志却什么都没存下来」。只有真写一次才作数。
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"建不出目录：{e}"
    name = ""
    try:
        fd, name = tempfile.mkstemp(prefix=".wtest-", dir=str(directory))
        try:
            os.write(fd, b"x")
        finally:
            os.close(fd)
    except OSError as e:
        return False, f"写不进：{e}"
    finally:
        if name:
            try:
                os.unlink(name)
            except OSError:
                pass
    return True, ""


# ══════════ data 目录可写性探测 ══════════
# 必须在 import 期做，而不是等 ensure_dirs()：SETTINGS / CONFIG / LOGS 这些常量会被
# settings、kernel、configgen 在 import 时用 `from .paths import ...` 绑走，
# 之后再改 paths.DATA 对它们没有任何影响 —— 回退只能在这里一次性定死。
FALLBACK_ACTIVE = False          # True = 已经回退到 %LOCALAPPDATA%\VPNTray\data
DATA_DIR_REASON = ""             # 回退/失败原因，原样贴进日志与界面提示
DATA_DIR = _PRIMARY_DATA

_write_ok, _write_why = _probe_writable(_PRIMARY_DATA)
if not _write_ok:
    _fallback = APPDATA_DIR / "data"
    _fb_ok, _fb_why = _probe_writable(_fallback)
    if _fb_ok:
        FALLBACK_ACTIVE = True
        DATA_DIR = _fallback
        DATA_DIR_REASON = f"{_PRIMARY_DATA} 不可写（{_write_why}），已改用 {_fallback}"
    else:
        # 两个都写不了：留在原处，让 ensure_dirs() 把失败清单报给上层，界面才好提示
        DATA_DIR_REASON = (
            f"{_PRIMARY_DATA} 不可写（{_write_why}）；"
            f"回退目录 {_fallback} 也不可写（{_fb_why}）"
        )
    try:
        import logging

        logging.getLogger("paths").warning("数据目录回退：%s", DATA_DIR_REASON)
    except Exception:  # noqa: BLE001
        pass

DATA = DATA_DIR
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
MODULES = DATA / "modules"        # 用户可自由增删的场景 / 规则模块（每模块一个 json）
LOCK_FILE = DATA / "app.lock"
DELAYS = DATA / "delays.json"          # 上次测速结果，重启/刷新面板后直接复用


def ensure_dirs() -> list[str]:
    """建好所有运行时目录，返回「建不出来」的清单（空列表 = 全部正常）。

    以前这里吞掉失败只记一行日志：装到 Program Files 后 data/logs 建不出来，
    界面一切正常，配置和日志却一个字节都没落盘，出了问题完全无从排查。
    现在把失败清单交回去，上层可以决定提示用户还是换目录。
    """
    failed: list[str] = []
    for d in (DATA, LOGS, UI_DIR, MODULES, APPDATA_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # 不能 import logger（会循环导入），只能走标准 logging
            import logging

            logging.getLogger("paths").warning("创建目录失败 %s: %s", d, e)
            failed.append(f"{d}: {e}")
    return failed


def fallback_state() -> dict[str, object]:
    """数据目录现状，供 App.state / 诊断页展示（只读，不要改返回值）。"""
    return {
        "data_dir": str(DATA),
        "data_dir_fallback": FALLBACK_ACTIVE,
        "data_dir_reason": DATA_DIR_REASON,
    }


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
    "MODULES", "LOCK_FILE", "ensure_dirs", "kernel_workdir_args", "asset",
    "APPDATA_DIR", "SYSProxy_BACKUP",
    "FALLBACK_ACTIVE", "DATA_DIR_REASON", "fallback_state",
]
