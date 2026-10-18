"""反馈系统：把「用户描述的问题 + 脱敏后的诊断信息 + 日志尾巴」整理成一份报告。

隐私是硬约束，不是可选项：
- **订阅链接就是凭证**（URL 里的 token 等于账号密码），整条打码，只留域名；
- 节点 password / uuid / 各协议的密钥同理；
- IP 打掉主机段，只留前两段（城市信息已经足够定位网络问题，完整 IP 属于个人数据）。

产出三样东西，缺一不可：
1. `data/feedback/<时间>.md` —— 用户自己能翻、能改、能贴到任何地方；
2. `data/feedback/<时间>-logs.txt` —— 完整日志，GitHub 链接塞不下时靠它当附件；
3. 一条 GitHub issue 预填链接 —— 点了就跳到已经填好标题和正文的新建 issue 页面。

不用 GitHub API 直接提交：那需要把 token 塞进客户端，等于把写权限发给所有人。
"""
from __future__ import annotations

import logging
import os
import platform
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .paths import DATA, ensure_dirs

log = logging.getLogger("feedback")

FEEDBACK_DIR = DATA / "feedback"

# GitHub 的 issue 预填走 URL，太长会被浏览器/服务端截断。留一半余量。
MAX_BODY_IN_URL = 5500
MAX_LOG_LINES = 400

# ── 脱敏 ────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r"""(https?://[^\s"'<>）)，。]+)""", re.I)
_IPV4_RE = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_IPV6_RE = re.compile(r"\b([0-9a-f]{1,4}(?::[0-9a-f]{0,4}){2,7})\b", re.I)
_SECRET_RE = re.compile(
    r"""((?:password|passwd|pwd|uuid|secret|token|auth|psk|key)\s*[:=]\s*)"""
    r"""(?:"[^"]*"|'[^']*'|[^\s,;}]+)""",
    re.I,
)


def _mask_url(u: str) -> str:
    """保留协议+域名，路径和 query 全打码 —— token 就在这两段里。"""
    m = re.match(r"(https?)://([^/?#]+)(.*)$", u, re.I)
    if not m:
        return "***"
    scheme, host, rest = m.group(1), m.group(2), m.group(3)
    if not rest or rest in ("/", ""):
        return f"{scheme}://{host}/"
    tail = "" if len(rest) <= 1 else "/***"
    return f"{scheme}://{host}{tail}"


def _mask_ip(m: re.Match[str]) -> str:
    return f"{m.group(1)}.{m.group(2)}.*.*"


def _mask_ipv6(m: re.Match[str]) -> str:
    head = m.group(1).split(":", 1)[0]
    return f"{head}::***"


def redact(text: str) -> str:
    """尽量把凭证和个人数据抹掉，同时保留「哪里错了」的信息量。"""
    if not text:
        return ""
    s = _URL_RE.sub(lambda m: _mask_url(m.group(1)), text)
    s = _SECRET_RE.sub(lambda m: m.group(1) + "***", s)
    s = _IPV4_RE.sub(_mask_ip, s)
    s = _IPV6_RE.sub(_mask_ipv6, s)
    # 本机用户名偶尔会出现在路径里
    try:
        home = str(Path.home())
        if home:
            s = s.replace(home, "%USERPROFILE%")
    except Exception:  # noqa: BLE001
        pass
    return s


# ── 收集 ────────────────────────────────────────────────────────────────
def _tail_log(lines: int = MAX_LOG_LINES) -> str:
    from . import logger as _lg

    path = getattr(_lg, "APP_LOG", None)
    if not path:
        return "（没有配置日志文件）"
    p = Path(path)
    if not p.exists():
        return f"（日志文件不存在：{p.name}）"
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            buf = f.readlines()[-lines:]
        return "".join(buf) or "（日志是空的）"
    except OSError as e:
        return f"（读日志失败：{e}）"


def _version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return "?"


def collect(app) -> str:
    """把「能帮上忙又不泄露隐私」的状态拼成一段 markdown。"""
    st = getattr(app, "state", {}) or {}
    s = getattr(app, "settings", {}) or {}
    sub = st.get("sub") or {}
    cur = st.get("current") or "—"
    kernel = getattr(app, "kernel", None)

    rows = [
        ("版本", _version()),
        ("界面", st.get("ui") or "?"),
        ("系统", f"{platform.system()} {platform.release()} ({platform.machine()})"),
        ("Python", platform.python_version()),
        ("时区", st.get("tz") or "?"),
        ("阶段", st.get("phase") or "?"),
        ("内核", "运行中" if (kernel and kernel.running()) else "未运行"),
        ("代理模式", st.get("mode") or "?"),
        ("当前节点", cur),
        ("节点总数", len(st.get("nodes") or [])),
        ("订阅模式", sub.get("mode") or "single"),
        ("订阅条数", len(sub.get("list") or [])),
        ("剩余流量", f"{sub.get('total_left_gb')} GB" if sub.get("total_left_gb") is not None else "未知"),
        ("AI 通道", "开" if (st.get("ai") or {}).get("enabled") else "关"),
        ("AI 节点", (st.get("ai") or {}).get("node") or "—"),
        ("系统代理开关", "开" if s.get("system_proxy") else "关"),
        ("已收藏节点", len(s.get("favorites") or [])),
    ]
    body = "\n".join(f"- {k}：{redact(str(v))}" for k, v in rows)
    err = st.get("error")
    if err:
        body += f"\n- 最近错误：{redact(str(err))}"
    return body


def build(app, title: str, detail: str, email: str = "") -> dict[str, Any]:
    """写文件 + 拼 issue 链接。不联网，纯本地。"""
    ensure_dirs()
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = FEEDBACK_DIR / f"{stamp}.md"
    log_path = FEEDBACK_DIR / f"{stamp}-logs.txt"

    title = (title or "").strip()[:120] or "使用问题反馈"
    detail = (detail or "").strip()
    diag = collect(app)
    tail = _tail_log()

    md = (
        f"# {title}\n\n"
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"- 联系方式：{redact(email) or '（未填）'}\n\n"
        f"## 问题描述\n\n{redact(detail) or '（未填）'}\n\n"
        f"## 诊断信息\n\n{diag}\n\n"
        f"## 日志（最后 {MAX_LOG_LINES} 行）\n\n"
        f"```\n{redact(tail)}\n```\n"
    )

    ok_file = True
    try:
        md_path.write_text(md, encoding="utf-8")
        log_path.write_text(redact(tail), encoding="utf-8")
    except OSError as e:
        ok_file = False
        log.warning("写反馈文件失败: %s", e)

    repo = str(app.settings.get("update_repo") or "").strip()
    url = ""
    if repo and "/" in repo:
        short = (
            f"{redact(detail) or '（未填）'}\n\n"
            f"---\n"
            f"### 自动附带的诊断信息\n\n{diag}\n\n"
            f"> 完整日志已保存到程序目录 `data/feedback/{stamp}-logs.txt`，"
            f"如果方便请把它一起贴上来（里面已自动去掉订阅链接、密码和完整 IP）。\n"
        )
        if len(short) > MAX_BODY_IN_URL:
            short = short[:MAX_BODY_IN_URL] + "\n\n…（已截断，完整内容见本地反馈文件）"
        q = urlencode({"title": f"[反馈] {title}", "body": short, "labels": "反馈"})
        url = f"https://github.com/{repo}/issues/new?{q}"

    log.info("已生成反馈文件 %s", md_path.name if ok_file else "（写入失败）")
    return {
        "ok": True,
        "file": str(md_path) if ok_file else "",
        "log_file": str(log_path) if ok_file else "",
        "dir": str(FEEDBACK_DIR),
        "issue_url": url,
        "stamp": stamp,
    }


def open_dir(path: str = "") -> bool:
    """打开反馈目录，用户能把日志拖进 issue。"""
    target = Path(path) if path else FEEDBACK_DIR
    try:
        if not target.exists():
            target = FEEDBACK_DIR
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(str(target))  # noqa: S606 —— Windows 专用
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("打开反馈目录失败: %s", e)
        return False


__all__ = ["build", "collect", "redact", "open_dir", "FEEDBACK_DIR"]
