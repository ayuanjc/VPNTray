"""本地 DNS 兜底（127.0.0.1:53）。

为什么需要这个文件：Windows 上有一类程序（Node/c-ares、Go 的 netgo 解析器、
部分 Electron 组件）不走系统缓存服务 DNS Client，而是自己判断「本机解析器」，
在注册表里读不到全局 NameServer 时就默认 127.0.0.1:53。本机没有东西监听 53
的时候，它们每一次域名解析都以 ECONNREFUSED 或 ETIMEDOUT 收场：

    cannot resolve tunnel destination dashscope.aliyuncs.com (ETIMEDOUT)
    local name resolution failed and a fresh resolver thread also failed

同一时刻浏览器却一切正常（它走 Dnscache → DHCP 下发的 211.136.192.6），
所以这种故障特别迷惑：网页能开，Node 工具和 AI 面板全报 502。

这里在 127.0.0.1:53 上架一个极小的 UDP/TCP 转发：查询原样发给国内公共 DNS，
应答原样回传，顺手做点缓存。三条硬约束：

1. **只在没人监听 53 时启动**。别的软件已经在提供本地解析就闭上嘴（绑定失败
   就安静退出），绝不抢端口、绝不改系统 DNS 设置。
2. **只监听 127.0.0.1**。不开局域网，不变成别人可以滥用的开放解析器。
3. **失败不抛出**。这块兜底挂了最多是回到原来的样子，不能让主程序起不来。
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
from typing import Any

log = logging.getLogger("dnsguard")

# 国内三家公共 DNS：任何一个活着都能解析国内外域名。
UPSTREAMS: tuple[str, ...] = ("223.5.5.5", "119.29.29.29", "114.114.114.114")
LISTEN_HOST = "127.0.0.1"
DEFAULT_PORT = 53
QUERY_TIMEOUT = 2.0
CACHE_MIN = 5
CACHE_MAX = 600
CACHE_MAX_ITEMS = 2000
MAX_INFLIGHT = 32
PROBE_HOST = "www.baidu.com"


def _build_query(name: str, qtype: int = 1) -> tuple[int, bytes]:
    """组一个 A/AAAA 查询包，返回 (事务 ID, 报文)。"""
    qid = int.from_bytes(os.urandom(2), "big")
    header = struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    body = b""
    for part in name.rstrip(".").split("."):
        raw = part.encode("idna") if part else b""
        body += bytes([len(raw)]) + raw
    body += b"\x00" + struct.pack("!HH", qtype, 1)
    return qid, header + body


def _skip_name(buf: bytes, i: int) -> int:
    """跳过（可能带压缩指针的）域名，返回其后第一个字节的下标。"""
    limit = len(buf)
    while i < limit:
        n = buf[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:      # 压缩指针，固定两字节
            return i + 2
        i += n + 1
    return limit


def _read_ttl(resp: bytes) -> int:
    """取应答里最小的 TTL 当缓存时长；解析不动就按 60 秒算。"""
    try:
        if len(resp) < 12:
            return 60
        qd, an = struct.unpack("!HH", resp[4:8])
        i = 12
        for _ in range(qd):
            i = _skip_name(resp, i) + 4
        ttl: int | None = None
        for _ in range(an):
            i = _skip_name(resp, i)
            if i + 10 > len(resp):
                break
            rtype, _rclass, rttl, rdlen = struct.unpack("!HHIH", resp[i:i + 10])
            i += 10 + rdlen
            if rtype == 41:        # OPT 伪记录，TTL 字段不是真实生存时间
                continue
            ttl = rttl if ttl is None else min(ttl, rttl)
        if ttl is None:
            return 60
        return max(CACHE_MIN, min(int(ttl), CACHE_MAX))
    except Exception:              # noqa: BLE001 —— 报文畸形时宁可按短 TTL 处理
        return 60


def _cache_key(data: bytes) -> bytes:
    """事务 ID 和标志位之后的整段：同一问题的不同 ID 命中同一条缓存。"""
    return data[2:]


def probe(port: int = DEFAULT_PORT, host: str = PROBE_HOST,
          timeout: float = 1.5) -> dict[str, Any]:
    """向本机 53 发一条真实查询，确认「本地解析器」这条路通不通。"""
    started = time.time()
    _qid, packet = _build_query(host)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (LISTEN_HOST, port))
        resp, _addr = sock.recvfrom(4096)
        ms = int((time.time() - started) * 1000)
        answers = 0
        try:
            answers = struct.unpack("!H", resp[6:8])[0]
        except Exception:          # noqa: BLE001
            pass
        return {"ok": True, "ms": ms, "answers": answers, "error": ""}
    except socket.timeout:
        return {"ok": False, "ms": int((time.time() - started) * 1000),
                "answers": 0, "error": "超时"}
    except OSError as e:
        return {"ok": False, "ms": int((time.time() - started) * 1000),
                "answers": 0, "error": f"{e.strerror or e}"}
    finally:
        sock.close()


def port_busy(port: int = DEFAULT_PORT) -> bool:
    """53 上是不是已经有人在服务（别的软件、或者本程序上一次的实例）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LISTEN_HOST, port))
        return False
    except OSError:
        return True
    finally:
        sock.close()


class DnsGuard:
    """127.0.0.1:53 上的转发器。线程都是 daemon，stop() 幂等。"""

    def __init__(self, port: int = DEFAULT_PORT,
                 upstreams: tuple[str, ...] = UPSTREAMS) -> None:
        self.port = int(port)
        self.upstreams = tuple(upstreams)
        self._lock = threading.Lock()
        self._cache: dict[bytes, tuple[float, bytes]] = {}
        self._udp: socket.socket | None = None
        self._tcp: socket.socket | None = None
        self._running = False
        self._threads: list[threading.Thread] = []
        self._inflight = threading.Semaphore(MAX_INFLIGHT)
        self.queries = 0
        self.hits = 0
        self.errors = 0
        self.upstream_ok = 0
        self.last_error = ""
        self.started_at = 0.0
        self.reason = ""           # 没起来的原因，给面板看

    # —— 生命周期 ——
    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._running:
            return True
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            udp.bind((LISTEN_HOST, self.port))
        except OSError as e:
            udp.close()
            self.reason = f"{self.port} 端口被占用（{e.strerror or e}），已有本地解析器"
            log.info("DNS 兜底未启动：%s", self.reason)
            return False
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            tcp.bind((LISTEN_HOST, self.port))
            tcp.listen(16)
        except OSError:
            tcp.close()
            tcp = None
        self._udp, self._tcp = udp, tcp
        self._running = True
        self.started_at = time.time()
        self.reason = ""
        self._threads = [threading.Thread(target=self._udp_loop, name="dnsguard-udp",
                                          daemon=True)]
        if tcp is not None:
            self._threads.append(threading.Thread(target=self._tcp_loop,
                                                  name="dnsguard-tcp", daemon=True))
        for t in self._threads:
            t.start()
        log.info("DNS 兜底已启动：%s:%d → %s", LISTEN_HOST, self.port,
                 ", ".join(self.upstreams))
        return True

    def stop(self) -> None:
        self._running = False
        for sock in (self._udp, self._tcp):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        self._udp = self._tcp = None
        if self.started_at:
            log.info("DNS 兜底已停止（共 %d 次查询，命中缓存 %d 次）",
                     self.queries, self.hits)

    # —— 转发核心 ——
    def _resolve(self, data: bytes) -> bytes | None:
        key = _cache_key(data)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                self.hits += 1
                # 事务 ID 必须换回请求方的那一个，否则客户端会丢包
                return data[:2] + hit[1][2:]
            if hit:
                self._cache.pop(key, None)
        for host in self.upstreams:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(QUERY_TIMEOUT)
            try:
                sock.sendto(data, (host, 53))
                resp, _ = sock.recvfrom(4096)
                if len(resp) < 12:
                    continue
                self.upstream_ok += 1
                ttl = _read_ttl(resp)
                with self._lock:
                    if len(self._cache) >= CACHE_MAX_ITEMS:
                        self._cache.clear()
                    self._cache[key] = (now + ttl, resp)
                return resp
            except OSError:
                continue
            finally:
                sock.close()
        self.errors += 1
        self.last_error = "上游全部无应答（" + ", ".join(self.upstreams) + "）"
        return None

    def _udp_loop(self) -> None:
        udp = self._udp
        while self._running and udp is not None:
            try:
                data, addr = udp.recvfrom(4096)
            except OSError:
                break
            if not data:
                continue
            self.queries += 1
            if not self._inflight.acquire(blocking=False):
                continue

            def handle(payload: bytes = data, peer: Any = addr) -> None:
                try:
                    resp = self._resolve(payload)
                    if resp:
                        udp.sendto(resp, peer)
                except OSError:
                    pass
                finally:
                    self._inflight.release()

            threading.Thread(target=handle, daemon=True).start()

    def _tcp_loop(self) -> None:
        tcp = self._tcp
        while self._running and tcp is not None:
            try:
                conn, _addr = tcp.accept()
            except OSError:
                break

            def handle(c: socket.socket = conn) -> None:
                try:
                    c.settimeout(QUERY_TIMEOUT + 3)
                    head = b""
                    while len(head) < 2:
                        chunk = c.recv(2 - len(head))
                        if not chunk:
                            return
                        head += chunk
                    need = struct.unpack("!H", head)[0]
                    body = b""
                    while len(body) < need:
                        chunk = c.recv(need - len(body))
                        if not chunk:
                            return
                        body += chunk
                    self.queries += 1
                    resp = self._resolve(body)
                    if resp:
                        c.sendall(struct.pack("!H", len(resp)) + resp)
                except OSError:
                    pass
                finally:
                    try:
                        c.close()
                    except OSError:
                        pass

            threading.Thread(target=handle, daemon=True).start()

    # —— 面板用 ——
    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "port": self.port,
            "listen": f"{LISTEN_HOST}:{self.port}",
            "upstreams": list(self.upstreams),
            "queries": self.queries,
            "hits": self.hits,
            "errors": self.errors,
            "upstream_ok": self.upstream_ok,
            "cache": len(self._cache),
            "last_error": self.last_error,
            "reason": self.reason,
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
        }


_guard: DnsGuard | None = None
_watch_stop = threading.Event()
_watch_thread: threading.Thread | None = None


def watch(interval: float = 20.0, enabled=None) -> None:
    """隔一会儿看一眼，别让兜底"悄悄死掉"。

    这个模块活在托盘程序里，一跑就是几天。中间可能遇到：被别家软件抢了 53 又放开、
    自己两个线程意外退出、上游短时全挂。启动时 ensure() 一次是不够的 —— 用户那边表现
    就是"本来好好的，睡一觉 AI 面板又开始 cannot resolve"。

    规则：没开就不起；端口被别人占着就不抢；自己活着但连查三次都答不出来就重启一遍。
    """
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return
    _watch_stop.clear()

    def on() -> bool:
        if enabled is None:
            return True
        try:
            return bool(enabled())
        except Exception:
            return True

    def loop() -> None:
        bad = 0
        while not _watch_stop.wait(interval):
            try:
                if not on():
                    if _guard is not None and _guard.running:
                        _guard.stop()
                    bad = 0
                    continue
                if _guard is None or not _guard.running:
                    ensure(port=_guard.port if _guard is not None else DEFAULT_PORT)
                    bad = 0          # 端口空就起来，被占就写 reason，不吵
                    continue
                r = probe(port=_guard.port, timeout=1.0)
                if r.get("ok"):
                    bad = 0
                    continue
                bad += 1
                _guard.last_error = str(r.get("error") or "")
                if bad >= 3:
                    log.warning("DNS 兜底连续 %d 次探测失败（%s），重启一次", bad, _guard.last_error)
                    _guard.stop()
                    time.sleep(0.3)
                    _guard.start()
                    bad = 0
            except Exception:
                pass

    _watch_thread = threading.Thread(target=loop, name="dnsguard-watch", daemon=True)
    _watch_thread.start()
    log.info("DNS 兜底自检已开：每 %.0f 秒一次", interval)


def unwatch() -> None:
    global _watch_thread
    _watch_stop.set()
    _watch_thread = None


def stop() -> None:
    """退出时把端口还回去。对象留着，下次 ensure() 直接重启同一份统计。"""
    unwatch()
    if _guard is not None:
        _guard.stop()


def ensure(port: int = DEFAULT_PORT, enabled: bool = True) -> DnsGuard:
    """全局唯一实例：需要就起，已经在跑就返回原对象。"""
    global _guard
    if _guard is None:
        _guard = DnsGuard(port=port)
    if not enabled:
        return _guard
    if not _guard.running:
        _guard.start()
    return _guard


def status() -> dict[str, Any]:
    if _guard is None:
        return {"running": False, "port": DEFAULT_PORT, "listen": "",
                "upstreams": list(UPSTREAMS), "queries": 0, "hits": 0,
                "errors": 0, "upstream_ok": 0, "cache": 0, "last_error": "",
                "reason": "尚未启动", "uptime": 0}
    return _guard.status()
