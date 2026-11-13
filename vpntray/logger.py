"""日志：文件 + 可选控制台。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .paths import APP_LOG, ensure_dirs

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 日志初始化失败时记下原因，UI 和托盘可以直接读
SETUP_ERROR = ""

# 打在 handler 上的标记：判断「本模块装过没有」只能靠它，不能看 root.handlers
# 是否为空（宿主程序可能已经先调过 basicConfig，那时早退会让日志文件永远不存在）
_TAG = "_vpntray_handler"


def _stderr_note(text: str) -> None:
    """往 stderr 写一行。windowed 打包后 sys.stderr 是 None，必须自己兜住。"""
    try:
        err = sys.stderr
        if err is not None:
            err.write(text + "\n")
            err.flush()
    except Exception:  # noqa: BLE001
        pass


class _SafeRotating(RotatingFileHandler):
    """Windows 上滚动日志时文件常被占用（用户拿记事本开着、杀毒在扫），
    标准实现会抛 PermissionError；更糟的是它抛之前已经把 self.stream 关掉/置空，
    此后每次 emit 都是 None.write，日志永久停摆，而且没人知道。

    这里把滚动失败降级成「这一轮不滚」并重新打开流，写入失败也重开一次并
    至少往 stderr 留一行，保证故障本身能被看见。
    """

    def doRollover(self) -> None:  # noqa: D102
        try:
            super().doRollover()
        except Exception as e:  # noqa: BLE001
            self._note("日志轮转失败", e)
            # 关键是重开：不管标准实现把 stream 留成 None 还是已关闭的对象，
            # 下一行日志都还有地方可写
            try:
                self.stream = self._open()
            except Exception as e2:  # noqa: BLE001
                self._note("日志重开失败", e2)
                self.stream = None

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            super().emit(record)
        except Exception as e:  # noqa: BLE001
            self._note("日志写入失败", e)
            try:
                if self.stream is None:
                    self.stream = self._open()
            except Exception:  # noqa: BLE001
                self.stream = None

    def _note(self, what: str, err: object) -> None:
        """同一类故障只播报一次，否则写日志失败会引发「每次写日志都报错」。"""
        if getattr(self, "_noted", "") == what:
            return
        self._noted = what
        _stderr_note(f"[vpntray] {what}: {err}")


def _our_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, _TAG, False)]


def setup(level: int = logging.INFO) -> logging.Logger:
    global SETUP_ERROR
    ensure_dirs()
    root = logging.getLogger()
    # 只要本模块装过就返回：以前用 `if root.handlers` 早退，宿主程序（pytest、
    # 被当库调用、先调过 basicConfig 的父进程）一旦先配好日志，我们的文件
    # handler 就再也装不上了
    if _our_handlers():
        return root
    root.setLevel(level)
    fmt = logging.Formatter(_FMT, datefmt="%Y-%m-%d %H:%M:%S")

    try:
        fh = _SafeRotating(APP_LOG, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        setattr(fh, _TAG, True)
        root.addHandler(fh)
    except Exception as e:  # noqa: BLE001
        # 日志文件打不开不能连累整个程序：先挂个空 handler，别让 logging 报
        # "No handlers found"；原因留给 UI 展示
        SETUP_ERROR = f"日志文件不可用：{e}"
        nh = logging.NullHandler()
        setattr(nh, _TAG, True)
        root.addHandler(nh)

    if sys.stderr is not None:
        try:
            tty = sys.stderr.isatty()
        except Exception:  # noqa: BLE001
            tty = False
        if tty:
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(fmt)
            setattr(ch, _TAG, True)
            root.addHandler(ch)

    # 第三方库降噪
    for noisy in ("urllib3", "requests", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if SETUP_ERROR:
        logging.getLogger("main").warning(SETUP_ERROR)
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
