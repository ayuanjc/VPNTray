"""本地 OCR 桥守护（ocrbridge）。

为什么单独一个模块
------------------
DSH 的「阿里云·OCR路由」把图片先送到 127.0.0.1:8791 换成文字，再转发给
DeepSeek。那个端口背后是一个 node 进程（ocr-router/server.mjs）。它没在听
的时候，模型调用直接 ECONNREFUSED，界面上看起来是「模型不可用」或者一直
转圈，跟 VPN、订阅一点关系都没有——而用户的第一反应永远是去重连节点。

托盘程序本来就常驻，所以这件事归这里：端口没人听就拉起来，掉了再拉，
失败只记一条日志，绝不抛出（守护自己挂掉最多少一次拉起，不能让主程序
起不来）。

边界
----
* 只拉起确认过存在 server.mjs 的目录，不猜命令、不跑别的东西。
* 退出 VPNTray 时不杀 node：OCR 桥和托盘生命周期解耦，重启托盘不该把
  正在跑的识别任务打断。
* 探活走 GET /health（1 秒超时），不走 TCP connect —— 端口被别的程序占着
  时 connect 同样成功，那是最容易误判成「活着」的假象。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ocrbridge")

DEFAULT_PORT = 8791
# 出厂落点：ocr-router 平时就在这个目录，settings.ocr_bridge_dir 可覆盖。
DEFAULT_DIR = Path(r"E:\Desktop\AI\wechat\ocr-router")
PROBE_TIMEOUT = 1.0
START_WAIT = 8.0
INTERVAL_MIN = 10
CREATE_NO_WINDOW = 0x08000000

_lock = threading.RLock()
_state: dict[str, Any] = {
    "ok": False,
    "reason": "未检查",
    "checked_at": 0.0,
    "dir": "",
    "port": DEFAULT_PORT,
    "started": 0,
}
_watch: threading.Thread | None = None
_watch_stop = threading.Event()


def _resolve_dir(hint: Any = None) -> Path:
    """找一个真带 server.mjs 的目录；都不成立就返回提示值，调用方会拒绝启动。"""
    for cand in (hint, os.environ.get("OCR_ROUTER_HOME"), DEFAULT_DIR,
                 Path.home() / "ocr-router", Path.cwd() / "ocr-router"):
        if not cand:
            continue
        try:
            p = Path(str(cand)).expanduser()
        except (OSError, ValueError):
            continue
        if (p / "server.mjs").is_file():
            return p
    return Path(str(hint or DEFAULT_DIR)).expanduser()


def health(port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """GET /health。返回 (是否真的在服务, 说明/模型名)。不抛出。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % int(port),
                                    timeout=PROBE_TIMEOUT) as r:
            code = getattr(r, "status", 200)
            body = r.read(512).decode("utf-8", "replace")
        if code != 200:
            return False, "HTTP %s" % code
        try:
            j = json.loads(body)
        except ValueError:
            return True, ""
        if isinstance(j, dict) and j.get("ok"):
            return True, str(j.get("ocrModel") or "")
        return False, str((j or {}).get("error") or "health 未返回 ok")
    except Exception as e:  # noqa: BLE001 探活失败一律当成「没活着」
        return False, "%s: %s" % (type(e).__name__, e)


def status() -> dict[str, Any]:
    with _lock:
        out = dict(_state)
    port = int(out.get("port") or DEFAULT_PORT)
    ok, why = health(port)
    out["ok"] = ok
    out["reason"] = why or "正常"
    # 端口已经有人服务时 _state.dir 还是空的（不是本进程拉起来的），
    # 面板要显示「桥在哪」，这里补一个猜测值；pid 从端口反查，省得记句柄。
    if ok:
        if not out.get("dir"):
            out["dir"] = str(_resolve_dir(None))
        out["pid"] = _pid_on_port(port)
        # health 返回的第二个值就是模型名（如 qwen3.5-ocr），面板直接显示它，
        # 免得用户为了确认「是不是那个模型」去翻 stdout.log。
        if why:
            out["model"] = why
    out["checked_at"] = time.time()
    with _lock:
        _state.update({"ok": ok, "reason": out["reason"], "checked_at": out["checked_at"]})
    return out


def start(*, dir: Any = None, port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """确保端口有人服务。已经活着就直接返回。不抛出。"""
    port = int(port or DEFAULT_PORT)
    ok, why = health(port)
    if ok:
        with _lock:
            _state.update({"ok": True, "reason": why or "已在运行", "checked_at": time.time(), "port": port})
        return True, "已在运行"

    d = _resolve_dir(dir)
    server = d / "server.mjs"
    if not server.is_file():
        msg = "找不到 server.mjs：%s" % d
        with _lock:
            _state.update({"ok": False, "reason": msg, "checked_at": time.time(), "dir": str(d), "port": port})
        log.warning("OCR 桥未启动：%s", msg)
        return False, msg

    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        msg = "PATH 里没有 node，无法拉起 OCR 桥"
        with _lock:
            _state.update({"ok": False, "reason": msg, "checked_at": time.time(), "dir": str(d), "port": port})
        log.warning(msg)
        return False, msg

    logs = d / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        logf = open(logs / "stdout.log", "a", encoding="utf-8", buffering=1)
    except OSError as e:
        msg = "日志打不开：%s" % e
        with _lock:
            _state.update({"ok": False, "reason": msg, "checked_at": time.time(), "dir": str(d), "port": port})
        log.warning("OCR 桥未启动：%s", msg)
        return False, msg

    try:
        proc = subprocess.Popen(
            [node, str(server)],
            cwd=str(d),
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        msg = "拉起失败：%s: %s" % (type(e).__name__, e)
        with _lock:
            _state.update({"ok": False, "reason": msg, "checked_at": time.time(), "dir": str(d), "port": port})
        log.warning("OCR 桥未启动：%s", msg)
        return False, msg
    finally:
        try:
            logf.close()
        except OSError:
            pass

    deadline = time.time() + START_WAIT
    tail = ""
    while time.time() < deadline:
        time.sleep(0.4)
        ok, tail = health(port)
        if ok:
            with _lock:
                _state.update({"ok": True, "reason": tail or "已就绪", "checked_at": time.time(),
                               "dir": str(d), "port": port, "started": time.time(),
                               "pid": getattr(proc, "pid", 0)})
            log.info("OCR 桥已就绪：127.0.0.1:%s（pid %s，%s）", port, getattr(proc, "pid", "?"), d)
            return True, tail or "已就绪"

    msg = "拉起后 %ss 内 /health 仍未就绪：%s" % (int(START_WAIT), tail)
    with _lock:
        _state.update({"ok": False, "reason": msg, "checked_at": time.time(), "dir": str(d), "port": port})
    log.warning("OCR 桥未就绪：%s", msg)
    return False, msg


def watch(*, interval: int = 30, dir: Any = None, port: int = DEFAULT_PORT,
          enabled: Callable[[], bool] | None = None,
          on_change: Callable[[bool, str], None] | None = None) -> None:
    """常驻自检：每 interval 秒看一眼，掉了就拉回来。重复调用只保留一个线程。"""
    global _watch
    try:
        step = max(INTERVAL_MIN, int(interval or 30))
    except (TypeError, ValueError):
        step = 30
    with _lock:
        if _watch is not None and _watch.is_alive():
            return
        _watch_stop.clear()

        def loop() -> None:
            last: bool | None = None
            while not _watch_stop.wait(step):
                try:
                    if enabled is not None and not enabled():
                        continue
                    ok, why = start(dir=dir, port=port)
                    if on_change is not None and (last is None or ok != last):
                        try:
                            on_change(ok, why)
                        except Exception:  # noqa: BLE001
                            log.debug("OCR 桥回调异常", exc_info=True)
                    last = ok
                except Exception:  # noqa: BLE001
                    log.debug("OCR 桥自检异常", exc_info=True)

        _watch = threading.Thread(target=loop, name="ocrbridge-watch", daemon=True)
        _watch.start()


def stop() -> None:
    """停自检线程。故意不杀 node：OCR 桥活多久与托盘无关。"""
    _watch_stop.set()
    with _lock:
        global _watch
        _watch = None


def _pid_on_port(port: int) -> int:
    """谁在 LISTEN 这个端口。只认 LISTENING 行，返回第一行的 PID（0=没有）。"""
    try:
        cp = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                            capture_output=True, timeout=10,
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:  # noqa: BLE001
        return 0
    text = (cp.stdout or b"").decode("gbk", "replace")
    want = ":%d" % int(port)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, state, pid = parts[1], parts[3].upper(), parts[4]
        if state != "LISTENING" or not local.endswith(want):
            continue
        try:
            return int(pid)
        except ValueError:
            continue
    return 0


def _image_of(pid: int) -> str:
    """进程映象名。拿不到就返回空串。"""
    if pid <= 0:
        return ""
    try:
        cp = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/FO", "CSV", "/NH"],
                            capture_output=True, timeout=10,
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:  # noqa: BLE001
        return ""
    text = (cp.stdout or b"").decode("gbk", "replace").strip()
    if not text or text.startswith("信息") or "INFO:" in text.upper():
        return ""
    first = text.splitlines()[0]
    name = first.split(",")[0].strip()
    return str(name).strip(chr(34) + " ").lower()


def kill(port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """按端口结束监听进程。只杀确实监听这个端口的 node.exe，不做通配。

    为什么要挑进程名：8791 有可能被别的程序占着（用户自己起的服务），
    按端口无脑 taskkill 会把别人的东西干掉。这里宁可失败也不误杀。
    """
    port = int(port or DEFAULT_PORT)
    pid = _pid_on_port(port)
    if not pid:
        return False, "端口 %d 上没有监听进程" % port
    if pid == os.getpid():
        return False, "端口 %d 是 VPNTray 自己占的，不动" % port
    name = _image_of(pid)
    if name and name not in ("node.exe", "node"):
        return False, "端口 %d 被 %s（pid %d）占着，不是 OCR 桥，不动它" % (port, name, pid)
    try:
        cp = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True, timeout=15,
                            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception as e:  # noqa: BLE001
        return False, "结束进程失败：%s: %s" % (type(e).__name__, e)
    if cp.returncode != 0:
        tail = (cp.stderr or cp.stdout or b"").decode("gbk", "replace").strip()
        return False, "taskkill 返回 %s：%s" % (cp.returncode, tail[:150])
    log.info("OCR 桥已结束（pid %s，端口 %s）", pid, port)
    return True, "已结束 pid %s" % pid


def restart(*, dir: Any = None, port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """先按端口杀，再拉起。stop() 不杀 node，所以这里必须自己动手。"""
    port = int(port or DEFAULT_PORT)
    killed, why = kill(port)
    if killed:
        time.sleep(0.5)
    ok, msg = start(dir=dir, port=port)
    if ok:
        return True, ("已重启（%s）" % why) if killed else "已拉起"
    return False, ("重启失败：%s；%s" % (why, msg)) if killed else msg


def ensure(*, dir: Any = None, port: int = DEFAULT_PORT) -> tuple[bool, str]:
    return start(dir=dir, port=port)
