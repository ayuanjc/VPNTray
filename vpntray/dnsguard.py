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

import ipaddress
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
MAX_TCP_CONNS = 16
PROBE_HOST = "www.baidu.com"


def _bind_exclusive(sock: socket.socket) -> None:
    """按平台选绑定选项：Windows 用独占，其它平台用可复用。

    Windows 上只设 SO_REUSEADDR 是危险的：别的进程可以抢同一个端口并「共享」它，
    内核按到达顺序把包分给其中一个 socket —— 表现为端口看起来在监听、一半查询
    却石沉大海。SO_EXCLUSIVEADDRUSE 能保证「要么我独占，要么我绑不上」，
    于是 port_busy() 也能说实话。
    """
    opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if opt is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 1)
            return
        except OSError:
            pass          # 老系统/受限环境不支持时退回下面的可复用写法
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def _recv_exact(sock: socket.socket, need: int) -> bytes | None:
    """读满 need 字节；对端提前关闭返回 None（TCP 长度前缀协议要用）。"""
    buf = b""
    while len(buf) < need:
        chunk = sock.recv(need - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _reply_ok(query: bytes, resp: bytes) -> bool:
    """应答是不是「这次查询」的有效回答：ID 对得上、QR=1、头部完整。

    不校验就等于把第一个到达的包当答案：本机别的解析器、上一个超时请求的
    迟到应答都会串进来，客户端拿到 ID 不对的包直接丢弃 —— 用户看到的是
    「偶尔解析不出来」，而日志里一片正常。
    """
    if len(resp) < 12:
        return False
    if resp[:2] != query[:2]:
        return False
    return bool(resp[2] & 0x80)


def _servfail(query: bytes) -> bytes:
    """上游全挂时给客户端一个规范 SERVFAIL。

    以前是干脆不回包，客户端要干等到自己的超时（有的实现 5 秒以上），
    而且分辨不出是本机解析器坏了还是网断了。回 SERVFAIL 至少立刻结束等待。
    """
    if len(query) < 12:
        return b""
    flags = 0x8182                       # QR=1 RD=1 RA=1 rcode=2(SERVFAIL)
    return (query[:2] + struct.pack("!H", flags) + query[4:6]
            + b"\x00\x00" * 3 + query[12:])


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


# ——— 交易域名不转发给国内 DNS（3.2.0）———
# 2026-11-13 的事故：223.5.5.5 把 www.okx.com 解成 CNAME awscn.okpool.top → 169.254.0.2
# （APIPA 黑洞）。nameserver-policy 只作用于 mihomo 自己的解析器，管不到这里的转发：
# 兜底原样把投毒答案回给客户端，于是走 127.0.0.1:53 的程序（Node/c-ares、Go netgo，
# 包括 okx_mm）全被送进黑洞，而浏览器/代理侧一切正常 —— 极难排查。
# 所以这些域名改走「经代理的 DoH」，取不到再退回原上游（至少比 SERVFAIL 好）。
DOH_DOMAINS = ("okx.com", "binance.com")
DOH_SERVERS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query")
DOH_TIMEOUT = 4.0
_DOH_PORT = 0          # 0 = 不知道混合端口，不做 DoH（启动时由 app 设置）


def set_doh_proxy(port: int) -> None:
    """告诉兜底走哪个本地混合端口去发 DoH（0/非法值＝关闭 DoH）。"""
    global _DOH_PORT
    try:
        _DOH_PORT = int(port or 0)
    except (TypeError, ValueError):
        _DOH_PORT = 0


def _qname(data: bytes) -> str:
    """从查询报文里取出问题域名（小写、无尾点）；取不到返回空串。"""
    if len(data) < 13:
        return ""
    labels: list[str] = []
    i = 12
    for _ in range(32):
        if i >= len(data):
            return ""
        n = data[i]
        if n == 0:
            return ".".join(labels).lower()
        if n & 0xC0:            # 问题段不该出现压缩指针
            return ""
        labels.append(data[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    return ""


def _doh_wanted(data: bytes) -> bool:
    name = _qname(data)
    return bool(name) and any(name == d or name.endswith("." + d) for d in DOH_DOMAINS)


# 这些网段出现在 A 记录里就说明是投毒/占位答案（169.254/16 是 APIPA 黑洞、
# 198.18/15 是 fake-ip、100.64/10 是运营商 NAT、0/8 与 127/8 是本机）：
# 客户端连上去不是立刻失败就是连错地方，比 SERVFAIL 更难查。
POISON_NETS = tuple(ipaddress.ip_network(n) for n in (
    "169.254.0.0/16", "0.0.0.0/8", "127.0.0.0/8", "198.18.0.0/15",
    "100.64.0.0/10", "224.0.0.0/4", "255.255.255.255/32"))


def _a_records(resp: bytes) -> list[str]:
    """列出应答里的 A 记录地址（只读报文，不缓存）。"""
    out: list[str] = []
    try:
        if len(resp) < 12:
            return out
        qd, an = struct.unpack("!HH", resp[4:8])
        i = 12
        for _ in range(qd):
            i = _skip_name(resp, i) + 4
        for _ in range(an):
            i = _skip_name(resp, i)
            if i + 10 > len(resp):
                break
            rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", resp[i:i + 10])
            i += 10
            if rtype == 1 and rdlen == 4 and i + 4 <= len(resp):
                out.append(".".join(str(b) for b in resp[i:i + 4]))
            i += rdlen
    except Exception:              # noqa: BLE001
        return out
    return out


def _poisoned(resp: bytes) -> bool:
    """应答里有没有黑洞地址（只对交易域名用）。"""
    for ip in _a_records(resp):
        try:
            if any(ipaddress.ip_address(ip) in net for net in POISON_NETS):
                return True
        except ValueError:
            continue
    return False


def _doh(data: bytes) -> bytes | None:
    """经本地混合端口（= 走代理）发一次 DoH，失败返回 None。"""
    if not _DOH_PORT:
        return None
    try:
        import requests            # 局部导入：没装也不能影响 UDP 转发
    except Exception:              # noqa: BLE001
        return None
    proxies = {"https": f"http://127.0.0.1:{_DOH_PORT}"}
    try:
        with requests.Session() as s:
            s.trust_env = False    # 环境里的 ALL_PROXY 不该改变这里的选路
            for url in DOH_SERVERS:
                try:
                    with s.post(url, data=data, proxies=proxies, timeout=DOH_TIMEOUT,
                                headers={"Content-Type": "application/dns-message",
                                         "Accept": "application/dns-message",
                                         "User-Agent": "VPNTray-dnsguard/1.0"}) as r:
                        if r.status_code != 200 or not r.content:
                            continue
                        if not _reply_ok(data, r.content):
                            continue     # ID 对不上就不算答案
                        return r.content
                except Exception:  # noqa: BLE001 —— 换下一个服务器
                    continue
    except Exception:              # noqa: BLE001
        return None
    return None


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
        _bind_exclusive(sock)
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
        self._tcp_conns = threading.Semaphore(MAX_TCP_CONNS)
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
        _bind_exclusive(udp)
        try:
            udp.bind((LISTEN_HOST, self.port))
        except OSError as e:
            udp.close()
            self.reason = f"{self.port} 端口被占用（{e.strerror or e}），已有本地解析器"
            log.info("DNS 兜底未启动：%s", self.reason)
            return False
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _bind_exclusive(tcp)
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
        # 关 socket 只会让 recvfrom/accept 抛错退出，线程本身还要一小会儿才结束；
        # 不等它们，紧随其后的 start() 会在旧线程还持着循环变量时重新绑端口。
        for t in self._threads:
            if t is not threading.current_thread() and t.is_alive():
                t.join(timeout=1.0)
        self._threads = []
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
        if _doh_wanted(data):
            resp = _doh(data)
            if resp is not None:
                ttl = _read_ttl(resp)
                with self._lock:
                    if len(self._cache) >= CACHE_MAX_ITEMS:
                        self._cache.clear()
                    self._cache[key] = (now + ttl, resp)
                return resp
            log.debug("交易域名 DoH 失败，退回国内上游：%s", _qname(data))
        last = ""
        for host in self.upstreams:
            resp = self._query_udp(host, data)
            if resp is None:
                last = f"{host} 无应答"
                continue
            if resp[2] & 0x02:
                # TC 位置位：UDP 装不下完整应答，按协议必须改走 TCP 重查，
                # 否则客户端拿到的是被截断的包，等于解析失败。
                tcp = self._query_tcp(host, data)
                if tcp is None:
                    last = f"{host} 应答被截断且 TCP 重查失败"
                    continue
                resp = tcp
            if _doh_wanted(data) and _poisoned(resp):
                # DoH 没成功才走到这里：国内上游把交易域名解成黑洞地址。
                # 这种答案绝不能回给客户端、也不能进缓存 —— 宁可 SERVFAIL
                # 让客户端立刻重试/报错，也好过连一个连不上的地址。
                last = f"{host} 返回黑洞地址 {','.join(_a_records(resp))}"
                log.warning("兜底拒绝上游投毒答案：%s via %s", _qname(data), host)
                continue
            rcode = resp[3] & 0x0F
            if rcode in (2, 5):
                # SERVFAIL / REFUSED 是「这家上游自己有问题」，不是答案；
                # 以前把它当成功应答缓存下来，用户会连续几分钟解析到 SERVFAIL。
                last = f"{host} 返回 rcode={rcode}"
                continue
            self.upstream_ok += 1
            ttl = _read_ttl(resp)
            with self._lock:
                if len(self._cache) >= CACHE_MAX_ITEMS:
                    self._cache.clear()
                self._cache[key] = (now + ttl, resp)
            return resp
        self.errors += 1
        self.last_error = "上游全部不可用（" + (last or "无应答") + "）"
        return None

    def _query_udp(self, host: str, data: bytes) -> bytes | None:
        """向单个上游查一次，带来源与报文校验（见 _reply_ok）。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(QUERY_TIMEOUT)
        try:
            sock.sendto(data, (host, 53))
            deadline = time.time() + QUERY_TIMEOUT
            while time.time() < deadline:
                resp, addr = sock.recvfrom(4096)
                if addr[0] != host:      # 不是我们问的那台，多半是串包
                    continue
                if not _reply_ok(data, resp):
                    continue
                return resp
        except OSError:
            return None
        finally:
            sock.close()
        return None

    def _query_tcp(self, host: str, data: bytes) -> bytes | None:
        """TCP 重查（RFC 1035：2 字节长度前缀）。只在 TC 置位时用。"""
        try:
            with socket.create_connection((host, 53), timeout=QUERY_TIMEOUT) as sock:
                sock.settimeout(QUERY_TIMEOUT)
                sock.sendall(struct.pack("!H", len(data)) + data)
                head = _recv_exact(sock, 2)
                if head is None:
                    return None
                need = struct.unpack("!H", head)[0]
                if need <= 0:
                    return None
                body = _recv_exact(sock, need)
                if body is None or not _reply_ok(data, body):
                    return None
                return body
        except OSError:
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
                    if not resp:
                        resp = _servfail(payload)
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
            # 连接数上限：以前每个连接一个线程且不设上限，一个死循环客户端
            # 就能把线程数堆到几千，进程直接被拖死。
            if not self._tcp_conns.acquire(blocking=False):
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            def handle(c: socket.socket = conn) -> None:
                try:
                    # 空闲超时：客户端连上却不发数据时，不能让它一直占着一个线程。
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
                    if not resp:
                        resp = _servfail(body)
                    if resp:
                        c.sendall(struct.pack("!H", len(resp)) + resp)
                except OSError:
                    pass
                finally:
                    try:
                        c.close()
                    except OSError:
                        pass
                    self._tcp_conns.release()

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
    t = _watch_thread
    _watch_stop.set()
    _watch_thread = None
    # 必须 join：不 join 的话快速开关会留下两个 watch 线程 —— 旧的还在 sleep，
    # 新的已经起来，同一时刻两个线程各自动 stop/start 同一个 guard。
    if t is not None and t is not threading.current_thread() and t.is_alive():
        t.join(timeout=3.0)


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
