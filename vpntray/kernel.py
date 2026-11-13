"""mihomo 内核进程托管：启动、就绪等待、崩溃自愈、退出清理。"""
from __future__ import annotations

import atexit
import hashlib
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


# 端口收敛只有一处实现（settings.port_setting），这里保留 _port 这个名字
# 供既有调用点使用。
from .settings import port_setting as _port


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    # 先直连探测：Windows 的 SO_REUSEADDR 允许绑定到别人正在监听的端口，
    # 只用 bind 判断会把「已被占用」误判成空闲。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        if probe.connect_ex((host, port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        # 关键：Windows 上 SO_REUSEADDR 允许绑定到别人正在监听的端口，
        # 绑上了就等于「空闲」，后面 Popen 出去的内核才报 address in use。
        # 独占模式才是真正的「这个端口现在能不能给我」。
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
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
    if not name.startswith("mihomo"):
        # 名字查不到（tasklist 失败 / 权限不足）时以前会盲杀，可能干掉用户自己的程序
        log.warning("端口被 %s 占用（PID %s），不是本程序的内核，不处理",
                    name or "未知进程", parts[1])
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


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_sidecar(dst: Path) -> None:
    """把已复制文件的哈希写到 <名字>.sha256，下次启动只需比对一次哈希。"""
    try:
        (DATA / (dst.name + ".sha256")).write_text(_digest(dst), encoding="ascii")
    except OSError as e:
        log.warning("写地理数据哈希失败（下次会重新比对全文）: %s", e)


def _same_content(src: Path, dst: Path) -> bool:
    """只比文件大小是不够的：同样大小的新版 geosite.dat 会被当成「已就位」，
    用户升级程序后规则悄悄停在半年前的版本上。这里比哈希，并留 sidecar 缓存。
    """
    try:
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            return False
        side = DATA / (dst.name + ".sha256")
        if side.exists():
            try:
                return side.read_text(encoding="ascii").strip() == _digest(src)
            except OSError:
                pass
        return _digest(src) == _digest(dst)
    except OSError:
        return False


def ensure_geo_assets() -> None:
    """mihomo 从 -d 目录读取地理数据，首次运行需要就位。"""
    ensure_dirs()
    for src in (GEOIP, GEOSITE, MMDB):
        if not src.exists():
            # country.mmdb 缺了不会立刻出事（configgen 会退回 geodata-mode=True
            # 用 GeoIP.dat），但这份警告要留着——它能解释「为什么地理规则变粗了」
            log.warning("缺少地理数据文件: %s", src)
            continue
        dst = DATA / src.name
        if not _same_content(src, dst):
            shutil.copyfile(src, dst)
            _write_sidecar(dst)
            log.info("地理数据就位: %s", dst.name)


KERNEL_LOG_MAX = 8 * 1024 * 1024   # kernel.log 超过这个大小就在启动前滚走


def _rotate_kernel_log() -> None:
    """mihomo 每条失败连接都写一行 warning，日志能长到几十 MB。

    子进程持有写句柄，运行中不能改名，所以只在启动前滚一次；滚不动就继续写
    原文件，绝不因为日志问题挡住内核启动。
    """
    try:
        if not KERNEL_LOG.exists() or KERNEL_LOG.stat().st_size <= KERNEL_LOG_MAX:
            return
        old = KERNEL_LOG.with_name(KERNEL_LOG.name + ".1")
        try:
            old.unlink()
        except FileNotFoundError:
            pass
        KERNEL_LOG.replace(old)
        log.info("kernel.log 超过 %d MB，已轮转为 %s",
                 KERNEL_LOG_MAX // (1024 * 1024), old.name)
    except OSError as e:
        log.warning("kernel.log 轮转失败（继续写原文件）: %s", e)


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
        # 锁只保护 self.proc 的读写：端口探测要跑两次 netstat（各 8s 超时），
        # 就绪等待最长 20s —— 这些都在锁里做的话，退出/重启会被硬拖住。
        with self._lock:
            if self.proc and self.proc.poll() is None:
                # 看门狗可能因为「连续崩溃放弃」已经退出，这里补一次，
                # 否则进程活着但没人看着，崩溃后再也不自愈。
                self._ensure_watch()
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
        _rotate_kernel_log()
        args = [str(MIHOMO), "-d", str(DATA), "-f", str(CONFIG)]
        logf = None
        with self._lock:
            # 端口检查到 Popen 之间可能被别的线程抢先起好，进锁后再确认一次
            if self.proc and self.proc.poll() is None:
                return True
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
            pid = self.proc.pid if self.proc else 0

        if not self._wait_ready(20.0):
            self.last_error = "内核启动后 API 无响应，请查看 data/logs/kernel.log"
            log.error(self.last_error)
            self._kill()
            return False

        if not _from_watchdog:
            self._restarts = 0     # 只有用户/程序主动启动成功才清零崩溃计数
        self.last_error = ""
        log.info("内核已就绪 pid=%s", pid)
        self._ensure_watch()
        return True

    def _ensure_watch(self) -> None:
        """看门狗只能有一个：重启路径反复 start() 绝不能把线程越堆越多。"""
        if self._watch and self._watch.is_alive():
            return
        if self._stop.is_set():     # 正在退出：别在关闭流程里再起一个循环
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
        # 句柄必须先留着：以前这里一进来就把 self.proc 置 None，terminate 抛异常
        # 或超时后 kill 也失败时，进程就彻底失联 —— 端口被占、系统代理指着它，
        # 而程序已经不知道该 PID 是谁。现在只有确认「已经死了或杀不掉」才放手。
        with self._lock:
            p = self.proc
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
            finally:
                if p.poll() is not None or self._stop.is_set():
                    if self.proc is p:
                        self.proc = None
            if p.poll() is None:
                log.error("内核进程仍在运行（pid=%s），保留句柄等待下次重试", p.pid)

    def _kill(self) -> None:
        self._terminate(wait=3.0)

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # ——— 自愈 ———
    def _watchdog(self) -> None:
        dead = 0        # 连续「进程还在、API 不应答」的次数
        while not self._stop.wait(1.5):
            p = self.proc
            if not p:
                return
            code = p.poll()
            if code is None:
                # 进程活着不等于内核可用：配置把 DNS 写死、监听队列打满、被系统
                # 挂起时进程照样在，9091 却再也不应答 —— 系统代理还指着这个死端口，
                # 表现为「网卡好好的，所有网页 502」。连续 3 次（≈4.5 秒）无应答才动手，
                # 避免把一次慢配置重载当成崩溃。
                if self.api.alive(timeout=1.5):
                    dead = 0
                else:
                    dead += 1
                    if dead < 3:
                        continue
                    log.error("内核进程在（pid=%s）但 API 连续 %d 次无应答，按崩溃处理",
                              p.pid, dead)
                    dead = 0
                    self._terminate(wait=3.0)
                    code = p.poll()
                    if code is None:
                        log.error("僵死内核杀不掉（pid=%s），保留句柄等下次重试", p.pid)
                        continue
            log.warning("内核异常退出 code=%s，准备重启", code)
            with self._lock:
                if self.proc is p:      # 别把别的线程刚拉起的新进程抹掉
                    self.proc = None
            if self._stop.is_set():
                return
            # 以前一次 start() 失败就 return，看门狗线程随之结束：内核死了没人管，
            # 系统代理还指着死端口 = 全网黑洞。这里重试到 _restarts 上限。
            while True:
                self._restarts += 1
                if self._restarts > 6:
                    self.last_error = "内核连续崩溃，已停止自动重启，请查看 kernel.log"
                    log.error(self.last_error)
                    return
                backoff = min(30, 2 ** self._restarts)
                if self._stop.wait(backoff):   # Event.wait：退出时立刻响应
                    return
                try:
                    ok = self.start(_from_watchdog=True)
                except Exception as e:  # noqa: BLE001
                    log.error("自动重启失败: %s", e)
                    ok = False
                if ok and self.running():
                    log.info("内核已自动重启（第 %d 次尝试）", self._restarts)
                    break
                log.warning("第 %d 次自动重启失败，%s 秒后重试: %s",
                            self._restarts, backoff, self.last_error or "原因未知")


__all__ = ["Kernel", "port_free", "port_owner_hint", "ensure_geo_assets"]
