"""mihomo 内核进程托管：启动、就绪等待、崩溃自愈、退出清理。"""
from __future__ import annotations

import atexit
import logging
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .paths import CONFIG, DATA, GEOIP, GEOSITE, KERNEL_LOG, LOGS, MIHOMO, MMDB, ensure_dirs

log = logging.getLogger("kernel")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _port(settings, key: str, default: int) -> int:
    """端口必须是 1024..65535 的整数，脏值回落默认值。"""
    try:
        v = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return v if 1024 <= v <= 65535 else default


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    # 先直连探测：Windows 的 SO_REUSEADDR 允许绑定到别人正在监听的端口，
    # 只用 bind 判断会把「已被占用」误判成空闲。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        if probe.connect_ex((host, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def port_owner_hint(port: int) -> str:
    """端口被占时给出「谁占的」提示。netstat/tasklist 输出是 GBK，必须显式解码。"""
    try:
        raw = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, timeout=8,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
        out = raw.decode("gbk", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pid = line.split()[-1]
            try:
                raw2 = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, timeout=8,
                    creationflags=CREATE_NO_WINDOW,
                ).stdout
                name = raw2.decode("gbk", errors="replace").strip().split(",")[0].strip('"')
                return f"PID {pid} {name}" if name else f"PID {pid}"
            except Exception:  # noqa: BLE001
                return f"PID {pid}"
    return ""


def _kill_pid_from_hint(hint: str) -> None:
    """port_owner_hint 返回 "PID 1234 mihomo.exe"，只对 mihomo 下手。

    端口被别的程序占着时（用户自己开过 clash、公司装了安全软件、甚至开发时
    起的一个临时服务），照着端口就 taskkill 会把人家的进程干掉。名字查不到
    才退回盲杀 —— 那种情况基本只有本程序留下的残留内核。
    """
    parts = hint.split()
    if len(parts) < 2 or parts[0] != "PID" or not parts[1].isdigit():
        return
    name = parts[2].lower() if len(parts) > 2 else ""
    if name and not name.startswith("mihomo"):
        log.warning("端口被 %s 占用（PID %s），不是本程序的内核，不处理", name, parts[1])
        return
    pid = parts[1]
    try:
        subprocess.run(
            ["taskkill", "/F", "/PID", pid],
            capture_output=True, timeout=8,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:  # noqa: BLE001
        pass


def ensure_geo_assets() -> None:
    """mihomo 从 -d 目录读取地理数据，首次运行需要就位。"""
    ensure_dirs()
    for src in (GEOIP, GEOSITE, MMDB):
        if not src.exists():
            log.warning("缺少地理数据文件: %s", src)
            continue
        dst = DATA / src.name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
            log.info("地理数据就位: %s", dst.name)


class Kernel:
    def __init__(self, api, settings) -> None:
        self.api = api
        self.settings = settings
        self.proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._watch: threading.Thread | None = None
        self._restarts = 0
        self.last_error: str = ""
        # 裸解释器退出 / 未捕获异常时的最后一道保险，
        # 否则 mihomo 会成为孤儿进程继续占端口、系统代理还指着它。
        atexit.register(self._atexit_kill)

    def _atexit_kill(self) -> None:
        try:
            p = self.proc
            if p and p.poll() is None:
                p.kill()
                try:
                    p.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    # ——— 生命周期 ———
    def start(self, _from_watchdog: bool = False) -> bool:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return True
            if not MIHOMO.exists():
                self.last_error = f"内核不存在: {MIHOMO}"
                log.error(self.last_error)
                return False
            if not CONFIG.exists():
                self.last_error = "配置尚未生成"
                log.error(self.last_error)
                return False

            port = _port(self.settings, "mixed_port", 7891)
            ctrl = _port(self.settings, "controller_port", 9091)
            for p_, why in ((port, "混合端口"), (ctrl, "控制端口")):
                if not port_free(p_):
                    owner = port_owner_hint(p_)
                    # 上一次异常退出（强杀/崩溃）会留下孤儿的 mihomo 占着端口，直接清掉
                    if "mihomo" in owner.lower():
                        log.warning("%s %s 被残留内核占用（%s），清理后重启", why, p_, owner)
                        _kill_pid_from_hint(owner)
                        for _ in range(20):
                            if port_free(p_):
                                break
                            time.sleep(0.25)
                    if not port_free(p_):
                        self.last_error = f"{why} {p_} 已被占用（{owner}），请关掉占用程序后重试"
                        log.error(self.last_error)
                        return False

            ensure_geo_assets()
            LOGS.mkdir(parents=True, exist_ok=True)
            args = [str(MIHOMO), "-d", str(DATA), "-f", str(CONFIG)]
            logf = None
            try:
                logf = open(KERNEL_LOG, "ab", buffering=0)
                logf.write(f"\n\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} start =====\n".encode())
                self.proc = subprocess.Popen(
                    args,
                    cwd=str(DATA),
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                )
            except Exception as e:  # noqa: BLE001
                # 日志文件被占用/磁盘满时 open 也会抛，这里必须一起兜住，
                # 否则异常会逃出 start()，直接把托盘启动流程炸掉。
                self.last_error = f"内核启动失败: {e}"
                log.error(self.last_error)
                return False
            finally:
                if logf is not None:
                    try:
                        logf.close()
                    except Exception:  # noqa: BLE001
                        pass

            self._stop.clear()
            if not self._wait_ready(20.0):
                self.last_error = "内核启动后 API 无响应，请查看 data/logs/kernel.log"
                log.error(self.last_error)
                self._kill()
                return False

            if not _from_watchdog:
                self._restarts = 0     # 只有用户/程序主动启动成功才清零崩溃计数
            self.last_error = ""
            log.info("内核已就绪 pid=%s", self.proc.pid)
            self._ensure_watch()
            return True

    def _ensure_watch(self) -> None:
        """看门狗只能有一个：重启路径反复 start() 绝不能把线程越堆越多。"""
        if self._watch and self._watch.is_alive():
            return
        self._watch = threading.Thread(target=self._watchdog, name="kernel-watch", daemon=True)
        self._watch.start()

    def _wait_ready(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False
            if self.api.alive(timeout=1.0):
                return True
            time.sleep(0.35)
        return False

    def stop(self, wait: float = 6.0) -> None:
        self._stop.set()
        with self._lock:
            self._terminate(wait)
        log.info("内核已停止")

    def _terminate(self, wait: float = 6.0) -> None:
        p = self.proc
        self.proc = None
        if not p:
            return
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=3)
        except Exception as e:  # noqa: BLE001
            log.warning("停止内核异常: %s", e)

    def _kill(self) -> None:
        self._terminate(wait=3.0)

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # ——— 自愈 ———
    def _watchdog(self) -> None:
        while not self._stop.wait(1.5):
            p = self.proc
            if not p:
                return
            if p.poll() is None:
                continue
            code = p.returncode
            log.warning("内核异常退出 code=%s，准备重启", code)
            with self._lock:
                if self.proc is p:      # 别把别的线程刚拉起的新进程抹掉
                    self.proc = None
            if self._stop.is_set():
                return
            self._restarts += 1
            if self._restarts > 6:
                self.last_error = "内核连续崩溃，已停止自动重启，请查看 kernel.log"
                log.error(self.last_error)
                return
            # 退避用 Event.wait 而不是 sleep，退出时能立刻响应
            if self._stop.wait(min(30, 2 ** self._restarts)):
                return
            with self._lock:
                if self._stop.is_set():
                    return
                try:
                    ok = self.start(_from_watchdog=True)
                except Exception as e:  # noqa: BLE001
                    log.error("自动重启失败: %s", e)
                    ok = False
                if not ok or not self.running():
                    if self.last_error:
                        log.error("自动重启放弃: %s", self.last_error)
                    return


__all__ = ["Kernel", "port_free", "port_owner_hint", "ensure_geo_assets"]
