"""AI 客户端（Claude Code / Codex CLI）专项：环境伪装与一致性检查。

背景（公开资料整理）：
  · Claude Code 会读取系统时区，用 Unicode 隐写把「时区 + 中转域名」编码进 system prompt；
  · 网页端/客户端的「AI 身份分」主要由 6 项加权：时区 30 / 浏览器语言 24 /
    中文字体 20 / Intl 区域 10 / 时区偏移 8 / Emoji 风格 8。

所以这里做三件能做的事：
  1. 读系统指纹，给出「中国特征分」和问题清单（让你看见问题在哪）；
  2. 把时区 / 语言做成「按国家」的一键切换（可还原），并生成
     `claude-safe.cmd` / `codex-safe.cmd` 启动脚本，用环境变量
     TZ + LANG 只影响 AI 进程，不动全局；
  3. 给出「哪些节点地区不适合用 Claude」的明确结论（UTC+8 一律不推荐）。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import winreg
from datetime import datetime
from pathlib import Path
from typing import Any

from .paths import APPDATA_DIR, DATA, ensure_dirs

log = logging.getLogger("disguise")

CREATE_NO_WINDOW = 0x08000000
# 时区备份必须放在「删 data 目录也不会丢」的地方：
# 还原时区是救命操作，备份跟着程序目录走就会在一次重装后永久丢失原时区。
BACKUP = APPDATA_DIR / "disguise_backup.json"
_LEGACY_BACKUP = DATA / "disguise_backup.json"

# ── 国家 → Windows 时区 ID / IANA 时区 ──
CC_TZ = {
    "US": ("Pacific Standard Time", "America/Los_Angeles"),
    "CA": ("Eastern Standard Time", "America/Toronto"),
    "GB": ("GMT Standard Time", "Europe/London"),
    "IE": ("GMT Standard Time", "Europe/Dublin"),
    "DE": ("W. Europe Standard Time", "Europe/Berlin"),
    "FR": ("Romance Standard Time", "Europe/Paris"),
    "NL": ("W. Europe Standard Time", "Europe/Amsterdam"),
    "CH": ("W. Europe Standard Time", "Europe/Zurich"),
    "SE": ("W. Europe Standard Time", "Europe/Stockholm"),
    "NO": ("W. Europe Standard Time", "Europe/Oslo"),
    "IT": ("W. Europe Standard Time", "Europe/Rome"),
    "ES": ("Romance Standard Time", "Europe/Madrid"),
    "PL": ("Central European Standard Time", "Europe/Warsaw"),
    "JP": ("Tokyo Standard Time", "Asia/Tokyo"),
    "KR": ("Korea Standard Time", "Asia/Seoul"),
    "AU": ("AUS Eastern Standard Time", "Australia/Sydney"),
    "NZ": ("New Zealand Standard Time", "Pacific/Auckland"),
    "BR": ("E. South America Standard Time", "America/Sao_Paulo"),
    "AE": ("Arabian Standard Time", "Asia/Dubai"),
    "TR": ("Turkey Standard Time", "Europe/Istanbul"),
    "IN": ("India Standard Time", "Asia/Kolkata"),
    # ⚠ UTC+8：时区本身就是最大的暴露面，列出来只是为了提示「别用」
    "SG": ("Singapore Standard Time", "Asia/Singapore"),
    "HK": ("China Standard Time", "Asia/Hong_Kong"),
    "TW": ("Taipei Standard Time", "Asia/Taipei"),
    "MO": ("China Standard Time", "Asia/Macau"),
    "MY": ("Singapore Standard Time", "Asia/Kuala_Lumpur"),
    "PH": ("Singapore Standard Time", "Asia/Manila"),
    "TH": ("SE Asia Standard Time", "Asia/Bangkok"),
    "VN": ("SE Asia Standard Time", "Asia/Ho_Chi_Minh"),
}

# UTC+8 地区：时区这一项洗不掉，用 Claude 属于高危
UTC8_CC = {"HK", "TW", "MO", "SG", "MY", "PH", "CN"}

# 用 Claude / Codex 时建议优先的地区（非 UTC+8 且落地干净）
RECOMMENDED = ["US", "JP", "KR", "GB", "DE", "NL", "FR", "CA", "AU"]

# 各国标准 UTC 偏移（夏令时 ±1h 的误差对「是不是 UTC+8」这种粗判没影响）
_CC_OFFSET: dict[str, float] = {
    "US": -5, "CA": -5, "MX": -6, "BR": -3, "AR": -3, "CL": -3,
    "GB": 0, "IE": 0, "PT": 0, "FR": 1, "DE": 1, "NL": 1, "BE": 1,
    "IT": 1, "ES": 1, "CH": 1, "SE": 1, "NO": 1, "DK": 1, "AT": 1,
    "PL": 1, "CZ": 1, "HU": 1, "RO": 2, "UA": 2, "TR": 3, "RU": 3,
    "AE": 4, "IN": 5.5, "TH": 7, "VN": 7, "ID": 7,
    "CN": 8, "HK": 8, "TW": 8, "MO": 8, "SG": 8, "MY": 8, "PH": 8,
    "JP": 9, "KR": 9, "AU": 10, "NZ": 12,
}


def tz_offset_hours(cc: str) -> float | None:
    """地区 → 标准 UTC 偏移。"""
    return _CC_OFFSET.get((cc or "").upper())


def local_offset_hours() -> float | None:
    """本机当前 UTC 偏移（不调外部命令，纯 Python）。"""
    off = datetime.now().astimezone().utcoffset()
    if off is None:
        return None
    return round(off.total_seconds() / 3600, 1)


def tz_risk(exit_cc: str) -> tuple[bool, str]:
    """(是否有风险, 人话说明)。

    注意：**时区一致 ≠ 安全**。本机是 UTC+8、节点也是 UTC+8 时两者「一致」，
    但 Claude 那边看到的仍然是 UTC+8——本身就指向中国/东南亚，照样危险。
    所以 UTC+8 一档无论是否一致都直接判风险。
    """
    cc = (exit_cc or "").upper()
    node_off = tz_offset_hours(cc)
    local_off = local_offset_hours()
    if node_off is None:
        return False, ""
    if cc in UTC8_CC:
        return True, (f"节点 {cc} 在 UTC+8 —— 这是中国/东南亚时区，Claude 眼里最可疑的一档；"
                      f"换 US / JP / KR / EU 节点再配合「时区对齐」")
    if local_off is not None and abs(node_off - local_off) < 0.5:
        return False, f"时区一致（都是 UTC{local_off:+g}），这一项不暴露"
    if local_off is None:
        return False, ""
    return True, (f"节点 {cc} 在 UTC{node_off:+g}，本机在 UTC{local_off:+g}"
                  f"—— 点「时区对齐」把本机改成 UTC{node_off:+g}，或改用启动脚本里的 TZ")


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout,
                             creationflags=CREATE_NO_WINDOW).stdout
        return out.decode("gbk", errors="replace").strip()
    except Exception as e:  # noqa: BLE001
        log.debug("命令失败 %s: %s", cmd, e)
        return ""


def _reg(path: str, name: str) -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return str(v)
    except OSError:
        return ""


# ────────────────────────── 读取本机指纹 ──────────────────────────
def read_fingerprint() -> dict[str, Any]:
    tz_id = _run(["tzutil", "/g"])
    locale_name = _reg(r"Control Panel\International", "LocaleName")
    locale_lang = _reg(r"Control Panel\International", "sLanguage")
    ui_lang = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-UICulture).Name"])
    font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    cn_fonts = [f for f in ("msyh.ttc", "msyhbd.ttc", "simsun.ttc", "simhei.ttf", "deng.ttf")
                if (font_dir / f).exists()]
    return {
        "tz_id": tz_id,
        "locale": locale_name,
        "lang": locale_lang,
        "ui_culture": ui_lang,
        "cn_fonts": cn_fonts,
    }


def _tz_offset_hours(tz_id: str) -> float | None:
    """用 PowerShell 查时区当前 UTC 偏移。"""
    if not tz_id:
        return None
    # 不能直接 %r：那是 Python 的转义规则（\'），PowerShell 单引号串里
    # 只认双写（''）。带引号的时区名会拼出一条能执行任意命令的脚本。
    lit = "'" + str(tz_id).replace("'", "''") + "'"
    script = (
        "[System.TimeZoneInfo]::FindSystemTimeZoneById(" + lit + ")"
        ".GetUtcOffset([DateTime]::UtcNow).TotalHours"
    )
    out = _run(["powershell", "-NoProfile", "-Command", script])
    try:
        return float(out)
    except (TypeError, ValueError):
        return None


# ────────────────────────── 中国特征评分 ──────────────────────────
def score(fp: dict[str, Any], exit_cc: str = "") -> dict[str, Any]:
    """0–100，越低越不像中国用户。权重参照公开的 6 信号模型。"""
    hits: list[dict[str, Any]] = []
    total = 0
    exit_cc = (exit_cc or "").strip().upper()

    off = _tz_offset_hours(fp.get("tz_id", ""))
    if off is not None and abs(off - 8) < 0.01:
        total += 30
        hits.append({"score": 30, "item": "系统时区为 UTC+8", "fix": "改成 US/JP/EU 时区，或只用启动脚本里的 TZ"})
    if off is not None and abs(off - 8) < 0.01:
        total += 8
        hits.append({"score": 8, "item": "时区偏移 +480 分钟", "fix": "同上"})

    lang = (fp.get("locale") or "").lower()
    ui = (fp.get("ui_culture") or "").lower()
    if lang.startswith("zh") or ui.startswith("zh"):
        total += 24
        hits.append({"score": 24, "item": f"区域/界面语言为中文（{fp.get('locale') or fp.get('ui_culture')}）",
                     "fix": "Windows 设置里加一个英文语言并置顶"})

    if fp.get("cn_fonts"):
        total += 20
        hits.append({"score": 20, "item": f"安装了中文字体（{len(fp['cn_fonts'])} 个）",
                     "fix": "不建议卸载；改不掉就看其它项"})

    # 出口地区与系统时区不一致 → 反而更容易被判定为「代理用户」
    if exit_cc and off is not None:
        want = CC_TZ.get(exit_cc)
        if want:
            expected_off = _tz_offset_hours(want[0])
            if expected_off is not None and abs(expected_off - off) >= 1:
                hits.append({"score": 0, "item": f"出口在 {exit_cc}，但系统时区偏移 {off:+.0f}h 与当地不符",
                             "fix": "点「一键对齐」把时区切到出口国家"})

    total = min(100, total)
    grade = "低" if total < 30 else ("中" if total < 60 else "高")
    return {"score": total, "grade": grade, "hits": hits,
            "tz_id": fp.get("tz_id"), "offset": off,
            "locale": fp.get("locale"), "cn_fonts": len(fp.get("cn_fonts") or [])}


# ────────────────────────── 时区切换（可还原） ──────────────────────────
def _load_backup() -> dict | None:
    for p in (BACKUP, _LEGACY_BACKUP):
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except Exception:  # noqa: BLE001
            continue
    return None


def _save_backup(old_tz: str) -> None:
    try:
        ensure_dirs()
        BACKUP.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"tz_id": old_tz}, ensure_ascii=False)
        tmp = BACKUP.with_name(BACKUP.name + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, BACKUP)
    except Exception as e:  # noqa: BLE001
        log.warning("时区备份写入失败: %s", e)


def current_tz() -> str:
    return _run(["tzutil", "/g"])


def set_timezone_for(cc: str) -> tuple[bool, str]:
    """把系统时区切到出口国家对应时区，原值记下来可还原。"""
    cc = (cc or "").upper()
    if cc not in CC_TZ:
        return False, f"没有 {cc} 的时区映射"
    tz_id, _ = CC_TZ[cc]
    old = current_tz()
    if not old:
        # 读不到当前时区就不动手：没有原值，之后「还原时区」就无从还原，
        # 用户会被永久留在别的时区上，比不切换糟得多
        return False, "读不到当前时区（tzutil 不可用），已跳过切换"
    if not _load_backup():
        _save_backup(old)
    _run(["tzutil", "/s", tz_id])
    now = current_tz()
    ok = bool(now) and now.lower() == tz_id.lower()
    log.info("时区 %s -> %s (%s)", old, tz_id, "成功" if ok else "失败")
    return ok, (f"时区已切到 {tz_id}" if ok else "切换失败，可能需要管理员权限")


def restore_timezone() -> tuple[bool, str]:
    bak = _load_backup() or {}
    tz = bak.get("tz_id")
    if not tz:
        return False, "没有可还原的时区备份"
    _run(["tzutil", "/s", tz])
    now = current_tz()
    ok = bool(now) and now.lower() == str(tz).lower()
    if ok:
        # 还原成功就清掉备份，否则下次「设置时区」会把软件自己的时区当成原值备份进去
        for p in (BACKUP, _LEGACY_BACKUP):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
    return ok, f"已还原为 {now or tz}"


# ────────────────────────── 启动脚本（只影响 AI 进程） ──────────────────────────
LAUNCH_DIR = DATA / "ai-launchers"

TEMPLATE = """@echo off
chcp 65001 >nul
rem ===== 由 VPN Tray 生成：只对本次会话生效，不改系统设置 =====
set "TZ={iana}"
set "LANG=en_US.UTF-8"
set "LC_ALL=en_US.UTF-8"
set "LANGUAGE=en_US"
rem AI 命令行工具不会自动读 Windows 的「系统代理」设置，只认这几个环境变量。
rem 不设的话 codex / claude 会走直连，表现就是「开了代理还是连不上」。
set "HTTPS_PROXY=http://127.0.0.1:{port}"
set "HTTP_PROXY=http://127.0.0.1:{port}"
set "ALL_PROXY=http://127.0.0.1:{port}"
set "NO_PROXY=127.0.0.1,localhost,::1"
{extra}
if "%~1"=="" goto run
{exe} %*
goto :eof
:run
{exe}
"""


LAUNCH_TOOLS = {"claude": "claude", "codex": "codex"}


def make_launcher(cc: str, tool: str = "claude", local_port: int = 7891) -> tuple[bool, str]:
    """生成 claude-safe.cmd / codex-safe.cmd。

    tool 必须来自白名单，绝不参与路径拼接（否则 ../.. 可写到启动目录）。
    local_port 是内核的混合端口，写进脚本的 HTTPS_PROXY。
    """
    cc = (cc or "").upper()
    tool = (tool or "").strip().lower()
    if tool not in LAUNCH_TOOLS:
        return False, f"未知工具：{tool or '(空)'}"
    if cc not in CC_TZ:
        return False, f"没有 {cc or '未知地区'} 的时区映射，未生成启动脚本"
    try:
        port = int(local_port)
    except (TypeError, ValueError):
        port = 7891
    if not 1 <= port <= 65535:
        port = 7891
    iana = CC_TZ[cc][1]
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    exe = LAUNCH_TOOLS[tool]
    name = {"claude": "claude-safe.cmd", "codex": "codex-safe.cmd"}[tool]
    path = LAUNCH_DIR / name
    extra = ""
    if tool == "claude":
        extra = ('rem 用官方端点最稳；用第三方中转时 Claude Code 会把时区写进 system prompt\n'
                 'if not defined ANTHROPIC_BASE_URL set "ANTHROPIC_BASE_URL=https://api.anthropic.com"')
    body = TEMPLATE.format(iana=iana, extra=extra, exe=exe, port=port)
    tmp = path.with_name(path.name + ".tmp")
    # 用 write_bytes 而不是 write_text：write_text 在 Windows 上会把 \n 再转一次
    # os.linesep，拼上这里手动加的 \r\n 就变成 \r\r\n，cmd 解析时行尾多一个 CR。
    tmp.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    tmp.replace(path)
    return True, str(path)


# ────────────────────────── 面向 UI 的汇总 ──────────────────────────
def inspect(exit_cc: str = "") -> dict[str, Any]:
    fp = read_fingerprint()
    sc = score(fp, exit_cc)
    sc["recommended"] = RECOMMENDED
    sc["utc8_warning"] = exit_cc in UTC8_CC
    sc["tz_map"] = {cc: CC_TZ[cc][0] for cc in CC_TZ}
    sc["launchers"] = sorted(p.name for p in LAUNCH_DIR.glob("*.cmd")) if LAUNCH_DIR.exists() else []
    return sc
