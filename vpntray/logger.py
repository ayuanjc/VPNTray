"""日志：文件 + 可选控制台。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .paths import APP_LOG, ensure_dirs

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 日志初始化失败时记下原因，UI 和托盘可以直接读
SETUP_ERROR = ""


class _SafeRotating(RotatingFileHandler):
    """Windows 上滚动日志时文件常被占用（用户拿记事本打开着、杀毒在扫），
    标准实现会抛 PermissionError，此后每次 emit 都再抛一次，日志永久停摆。
    这里把滚动失败降级成「这一轮不滚」，写入失败直接忽略。"""

    def doRollover(self) -> None:  # noqa: D102
        try:
            super().doRollover()
        except Exception:  # noqa: BLE001
            pass

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            super().emit(record)
        except Exception:  # noqa: BLE001
            pass


def setup(level: int = logging.INFO) -> logging.Logger:
    global SETUP_ERROR
    ensure_dirs()
    root = logging.getLogger()
    if root.handlers:
        return root
    root.setLevel(level)
    fmt = logging.Formatter(_FMT, datefmt="%Y-%m-%d %H:%M:%S")

    try:
        fh = _SafeRotating(APP_LOG, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:  # noqa: BLE001
        # 日志文件打不开不能连累整个程序：先挂个空 handler，别让 logging 报 "No handlers"
        SETUP_ERROR = f"日志文件不可用：{e}"
        root.addHandler(logging.NullHandler())

    if sys.stderr is not None and sys.stderr.isatty():
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    # 第三方库降噪
    for noisy in ("urllib3", "requests", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if SETUP_ERROR:
        logging.getLogger("main").warning(SETUP_ERROR)
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
