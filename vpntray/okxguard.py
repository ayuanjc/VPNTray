"""交易通道守护（OKX / 币安）。

为什么单独一个模块：做市的进程（okx_mm）跑在后台，出问题时的症状是
「下单返回 50113 签名失败 / 50101 环境不匹配 / 白名单拒绝」或者满屏
Cannot connect to host www.okx.com:443，而不是「网页打不开」。等看到报错时，
往往已经错过几十个报价周期，甚至挂着一堆过期单。

这里每 okx_guard_interval 秒（默认 60）探四件事，把故障提前变成一条托盘通知：

1. 出口 IP。OKX 的 API Key 可以绑 IP（账户安全里设白名单），出口一变，
   私有接口就可能直接拒绝。只告警不自动换 —— 换 IP 本身有代价。
2. REST 延迟。GET /api/v5/public/time 经代理计时；热连接超过 SLOW_MS 说明
   挂单已经过期、撤单也来不及。
3. WS 可达性。ws.okx.com:8443 走 HTTP CONNECT 探测；连不上等于行情断流，
   而断流时做市会按最后一笔价继续报价。
4. 解析是否被投毒（3.2.0 新增）。这是 2026-11-13 那次事故的教训：
   系统 DNS 把 www.okx.com 解成 CNAME awscn.okpool.top -> 169.254.0.2（APIPA 黑洞），
   阿里 DoH 也脏，连本程序自己的 127.0.0.1:53 兜底转发都脏。只有 mihomo 内部
   走 nameserver-policy 的那条路是真值。以前这里只查 REST/WS，而 REST 探针本身
   走代理 CONNECT（由 mihomo 解析），所以「代理通、直连死」这种状态完全看不出来。

设计约束（都不许违反）：
- 只读。不启停内核、不改节点、不动系统代理、不碰账户（唯一的动作是投毒时
  让 mihomo 重载一次配置，那本来就是它自己的文件）。
- 边沿触发。故障只在上一次健康时告警一次；持续故障不刷屏，恢复时补一条。
- 失败绝不抛出。守护自己挂掉最多是少一条告警，不能让主程序起不来。
- 走代理取数。出口 IP / REST 都必须经过 mixed-port，否则测的是家宽直连。
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import threading
import time
from typing import Any

import requests

log = logging.getLogger("okxguard")

# 探活最短间隔。低于 10 秒没意义：ip-api 免费额度 45 次/分钟。
INTERVAL_MIN = 10
# REST 热连接超过这个毫秒数算「慢」。冷连接（DNS+TLS+CONNECT）实测 830~1200ms，
# 用冷数字比阈值会让每个节点都长期超速，告警就废了。
SLOW_MS = 800
WS_TIMEOUT = 6.0
IP_TTL = 900
IP_URL = "http://ip-api.com/json/?fields=status,query,country,city,isp"
REST_URL = "https://www.okx.com/api/v5/public/time"
WS_HOST = "ws.okx.com"
WS_PORT = 8443

# 要盯的交易域名。不加 www.baidu.com 这类：它们被投毒的概率低，
# 而且每次多查一条就多一条超时风险。
TRADE_DOMAINS = ("www.okx.com", "app.okx.com", "ws.okx.com", "www.binance.com")

# 出现在公网域名应答里的「黑洞 / 私有」网段。169.254 是 APIPA，
# 198.18 是 fake-ip（mihomo 的默认段，被系统 DNS 当成真地址就是投毒）。
BAD_NETS = tuple(ipaddress.ip_network(x) for x in (
    "169.254.0.0/16", "0.0.0.0/8", "127.0.0.0/8",
    "198.18.0.0/15", "100.64.0.0/10", "224.0.0.0/4", "255.255.255.255/32",
))

_STATE: dict[str, Any] = {
    "checked_at": 0.0,
    "ip": "",
    "ip_from": "",
    "country": "",
    "rest_ms": 0,
    "rest_cold_ms": 0,
    "ws_ok": None,
    "dns_ok": None,          # 内核侧解析是否干净（True/False/None=没探到）
    "dns_sys_ok": None,      # 系统 DNS（含本机 53 兜底）是否干净
    "dns_detail": [],        # [{domain, via, ips, bad, err}]
    "dns_bad": [],           # 被投毒的域名
    "error": "",
}
_ALERTED: dict[str, float] = {}
_SYS_CACHE: dict[str, Any] = {"ts": 0.0, "servers": ("223.5.5.5",)}
_LOCK = threading.Lock()
_STOP = threading.Event()
_THREAD: threading.Thread | None = None


# ---------- 基础探测 ----------

def _proxies(port: int) -> dict[str, str]:
    url = f"http://127.0.0.1:{port}"
    return {"http": url, "https": url}


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False            # 忽略环境里的 ALL_PROXY，只用我们指的那个端口
    s.headers["User-Agent"] = "VPNTray/1.0"
    return s


def probe_ip(port: int) -> tuple[str, str, str]:
    """经代理查出口 IP。返回 (ip, country, error)。"""
    try:
        with _session() as s:
            with s.get(IP_URL, proxies=_proxies(port), timeout=8) as r:
                if r.status_code != 200:
                    return "", "", f"http {r.status_code}"
                data = r.json()
                if not isinstance(data, dict) or data.get("status") != "success":
                    return "", "", str(data.get("message") or "ip-api 失败")
                return str(data.get("query") or ""), str(data.get("country") or ""), ""
    except Exception as e:  # noqa: BLE001
        return "", "", type(e).__name__


def probe_rest(port: int) -> tuple[int, int, str]:
    """经代理打 OKX 公共时间接口，返回 (热连接毫秒, 冷连接毫秒, 错误)。"""
    t0 = time.monotonic()
    cold = 0
    try:
        with _session() as s:
            try:
                with s.get(REST_URL, proxies=_proxies(port), timeout=10):
                    pass
            except Exception:  # noqa: BLE001
                pass
            cold = int((time.monotonic() - t0) * 1000)
            t1 = time.monotonic()
            with s.get(REST_URL, proxies=_proxies(port), timeout=10) as r:
                ms = int((time.monotonic() - t1) * 1000)
                if r.status_code != 200:
                    return ms, cold, f"http {r.status_code}"
                return ms, cold, ""
    except Exception as e:  # noqa: BLE001
        return int((time.monotonic() - t0) * 1000), cold, type(e).__name__


def probe_ws(port: int) -> tuple[bool | None, str]:
    """用 HTTP CONNECT 探 ws.okx.com:8443 是否可建连。None 表示没探（不是失败）。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=WS_TIMEOUT) as s:
            s.settimeout(WS_TIMEOUT)
            crlf = chr(13) + chr(10)
            req = f"CONNECT {WS_HOST}:{WS_PORT} HTTP/1.1{crlf}Host: {WS_HOST}:{WS_PORT}{crlf}{crlf}"
            s.sendall(req.encode("ascii"))
            buf = s.recv(256)
            return (b" 200 " in buf or buf.startswith(b"HTTP/1.1 200")), ""
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


# ---------- 解析检查（3.2.0） ----------

def is_poison(ip: str) -> bool:
    """黑洞 / 私有 / fake-ip 地址出现在公网域名应答里就是投毒。"""
    try:
        addr = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return False
    return any(addr in net for net in BAD_NETS)


def _build_query(name: str, qtype: int = 1) -> tuple[int, bytes]:
    """手搓一条 DNS 查询（A / CNAME）。txid 用当前时间低 16 位，够用且免依赖。"""
    txid = int(time.time() * 1000) & 0xFFFF
    header = struct.pack(">HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    q = b""
    for part in str(name).rstrip(".").split("."):
        raw = part.encode("ascii")
        q += bytes([len(raw)]) + raw
    q += bytes([0]) + struct.pack(">HH", qtype, 1)
    return txid, header + q


def _skip_name(buf: bytes, off: int) -> int:
    while off < len(buf):
        ln = buf[off]
        if ln == 0:
            return off + 1
        if ln & 0xC0 == 0xC0:
            return off + 2
        off += 1 + ln
    return off


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = 0
    start = off
    for _ in range(32):
        if off >= len(buf):
            break
        ln = buf[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:
            if not jumped:
                start = off + 2
            off = ((ln & 0x3F) << 8) | buf[off + 1]
            jumped = 1
            continue
        labels.append(buf[off + 1:off + 1 + ln].decode("ascii", "replace"))
        off += 1 + ln
    return ".".join(labels), (start if jumped else off)


def parse_answer(resp: bytes) -> tuple[list[str], list[str], int]:
    """返回 (A 记录, CNAME, rcode)；rcode != 0 表示应答无效。"""
    if len(resp) < 12:
        return [], [], -1
    rcode = resp[3] & 0x0F
    qd = struct.unpack(">H", resp[4:6])[0]
    an = struct.unpack(">H", resp[6:8])[0]
    off = 12
    for _ in range(qd):
        off = _skip_name(resp, off) + 4
    ips: list[str] = []
    cnames: list[str] = []
    for _ in range(max(0, an)):
        if off >= len(resp):
            break
        _name, off = _read_name(resp, off)
        if off + 10 > len(resp):
            break
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", resp[off:off + 10])
        off += 10
        rdata = resp[off:off + rdlen]
        if rtype == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in rdata))
        elif rtype == 5:
            # off 此刻正指向 rdata 起点（已经 +10），CNAME 的 rdata 就是一条域名。
            # 以前写成 off - rdlen：rdlen 是 rdata 长度而不是「name+固定头」的长度，
            # 于是从一段错误的偏移开始解码，拿回来的是一串二进制垃圾。
            cn, _ = _read_name(resp, off)
            cnames.append(cn)
        off += rdlen
    return ips, cnames, rcode


def _ask_udp(server: str, name: str, timeout: float = 2.0) -> tuple[list[str], list[str], int, str]:
    """向指定 DNS 服务器问一条 A 记录。"""
    txid, q = _build_query(name)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(q, (server, 53))
        resp, _peer = sock.recvfrom(2048)
    except Exception as e:  # noqa: BLE001
        return [], [], -1, type(e).__name__
    finally:
        sock.close()
    if len(resp) < 12 or struct.unpack(">H", resp[:2])[0] != txid:
        return [], [], -1, "应答不匹配"
    ips, cnames, rcode = parse_answer(resp)
    return ips, cnames, rcode, ""


def _system_servers(ttl: float = 600.0) -> tuple[str, ...]:
    """系统 DNS 列表（10 分钟内复用）。拿不到就退回阿里 —— 它这次也脏，但至少能证明全脏。"""
    now = time.time()
    cached = _SYS_CACHE.get("servers")
    if cached and now - float(_SYS_CACHE.get("ts") or 0) < ttl:
        return tuple(str(x) for x in cached)
    servers: tuple[str, ...] = ("223.5.5.5",)
    try:
        from . import netdoctor
        got = netdoctor._dns_servers()          # noqa: SLF001
        if isinstance(got, tuple) and got and got[0]:
            servers = tuple(str(x) for x in got[0] if str(x).strip()) or servers
    except Exception:  # noqa: BLE001
        pass
    _SYS_CACHE.update({"ts": now, "servers": servers})
    return servers


def _ask_kernel(app: Any, name: str, timeout: float = 4.0) -> tuple[list[str], str]:
    """问 mihomo 自己的解析器（/dns/query）—— 唯一经 nameserver-policy 的路。"""
    api = getattr(app, "api", None)
    if api is None or not hasattr(api, "_req"):
        return [], "无内核接口"
    try:
        data = api._req("GET", f"/dns/query?name={name}&type=A", timeout=timeout)   # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        return [], type(e).__name__
    if not isinstance(data, dict):
        return [], "应答格式异常"
    ans = data.get("Answer") or data.get("answer") or []
    ips = []
    for item in ans if isinstance(ans, list) else []:
        if not isinstance(item, dict):
            continue
        if int(item.get("type") or 0) != 1:
            continue
        val = str(item.get("data") or "").strip()
        if val:
            ips.append(val)
    return ips, ""


def probe_dns(app: Any) -> dict[str, Any]:
    """查交易域名在「内核」和「系统 DNS」两条路上的解析结果。

    - 内核侧脏 = 严重（nameserver-policy 被订阅覆盖 / 内核 DNS 挂了），会触发重载；
    - 只有系统侧脏 = 已知现状（直连会解析到黑洞），每次状态里如实标出来。
    """
    detail: list[dict[str, Any]] = []
    bad: list[str] = []
    kernel_ok: bool | None = None
    sys_ok: bool | None = None
    for dom in TRADE_DOMAINS:
        ips, err = _ask_kernel(app, dom)
        bad_ips = [ip for ip in ips if is_poison(ip)]
        if ips:
            kernel_ok = False if (bad_ips or kernel_ok is False) else True
        detail.append({"domain": dom, "via": "kernel", "ips": ips, "bad": bad_ips, "err": err})
        if bad_ips and dom not in bad:
            bad.append(dom)
        if dom != TRADE_DOMAINS[0]:
            continue                     # 系统侧只查一个代表域名，省时间
        for srv in _system_servers()[:2] + ("127.0.0.1",):
            s_ips, s_cn, s_rcode, s_err = _ask_udp(srv, dom, timeout=1.6)
            s_bad = [ip for ip in s_ips if is_poison(ip)]
            detail.append({"domain": dom, "via": srv, "ips": s_ips, "cnames": s_cn,
                           "bad": s_bad, "err": s_err or ("" if s_rcode == 0 else f"rcode={s_rcode}")})
            if s_ips:
                sys_ok = False if (s_bad or sys_ok is False) else True
            if s_bad and dom not in bad:
                bad.append(dom)
    return {"kernel_ok": kernel_ok, "sys_ok": sys_ok, "detail": detail, "bad": bad}


# ---------- 告警与主循环 ----------

def _once(name: str, gap: float) -> bool:
    """边沿触发的闸门：同一类告警在 gap 秒内只放一次。"""
    now = time.time()
    last = _ALERTED.get(name, 0.0)
    if now - last < gap:
        return False
    _ALERTED[name] = now
    return True


def _notify(app: Any, text: str, level: str = "warning") -> None:
    fn = getattr(app, "notify", None)
    if not callable(fn):
        return
    try:
        fn(text, level)
    except TypeError:
        try:
            fn(text)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _reload_kernel(app: Any) -> str:
    """让 mihomo 重载自己的配置（通常会把 DNS 连接和 fake-ip 表一起重建）。"""
    api = getattr(app, "api", None)
    reload_fn = getattr(api, "reload", None)
    if not callable(reload_fn):
        return "no-api"
    try:
        from .paths import CONFIG
        reload_fn(str(CONFIG))
        return "ok"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__


def tick(app: Any, st: Any) -> dict[str, Any]:
    """跑一轮探测并返回状态。任何异常都在内部消化。"""
    port = int(st.get("mixed_port", 7891) or 7891)
    ip, country, ip_err = probe_ip(port)
    rest_ms, rest_cold, rest_err = probe_rest(port)
    ws_ok, ws_err = probe_ws(port)
    dns = probe_dns(app)

    with _LOCK:
        prev = dict(_STATE)
        _STATE.update({
            "checked_at": time.time(),
            "ip": ip or prev.get("ip", ""),
            "country": country or prev.get("country", ""),
            "rest_ms": rest_ms,
            "rest_cold_ms": rest_cold,
            "ws_ok": ws_ok,
            "dns_ok": dns["kernel_ok"],
            "dns_sys_ok": dns["sys_ok"],
            "dns_detail": dns["detail"],
            "dns_bad": dns["bad"],
            "error": ip_err or rest_err or ws_err,
        })

    # 1. 出口 IP 漂移
    old_ip = str(_STATE.get("ip_from") or "")
    if ip and old_ip and ip != old_ip and _once("ip:" + ip, IP_TTL):
        _notify(app, f"OKX 出口 IP 变了：{old_ip} → {ip}（绑过 IP 白名单要更新）")
        log.warning("出口 IP 变化 %s -> %s", old_ip, ip)
    if ip and not old_ip:
        log.info("OKX 出口 IP 基线 %s (%s)", ip, country)
    if ip:
        _STATE["ip_from"] = ip

    # 2. REST 延迟
    if rest_err:
        if _once("rest:down", 300):
            _notify(app, f"OKX REST 不通：{rest_err}（下单/撤单都会失败）")
            log.error("OKX REST 探测失败: %s", rest_err)
    elif rest_ms >= SLOW_MS:
        if _once("rest:slow", 300):
            _notify(app, f"OKX REST 慢：{rest_ms} ms（冷连接 {rest_cold} ms，挂单已过期、撤单赶不上）")
            log.warning("OKX REST %d ms (cold %d)", rest_ms, rest_cold)
    else:
        if _ALERTED.pop("rest:down", None) or _ALERTED.pop("rest:slow", None):
            _notify(app, f"OKX REST 已恢复：{rest_ms} ms")
            log.info("OKX REST 恢复 %d ms", rest_ms)

    # 3. WS 可达性
    if ws_ok is False:
        if _once("ws:down", 300):
            _notify(app, "OKX 行情 WS 连不上（8443 建连失败），盘口会停在旧价")
            log.error("OKX WS 探测失败: %s", ws_err)
    elif ws_ok is True:
        if _ALERTED.pop("ws:down", None):
            _notify(app, "OKX 行情 WS 已恢复")
            log.info("OKX WS 恢复")

    # 4. 解析投毒
    kernel_bad = [d for d in dns["detail"] if d["via"] == "kernel" and d["bad"]]
    sys_bad = [d for d in dns["detail"] if d["via"] != "kernel" and d["bad"]]
    if kernel_bad:
        # 内核侧都脏说明 nameserver-policy 失效了：这是「代理也救不了」的状态
        if _once("dns:kernel", 600):
            first = kernel_bad[0]
            _notify(app, f"DNS 投毒已穿透内核：{first['domain']} → {', '.join(first['bad'])}（代理也会失败）")
            log.error("DNS 投毒（内核侧）：%s", kernel_bad)
            # 重载也跟着节流：每 60 秒重载一次配置等于一直重启内核，
            # 中间那几十秒全网抖动比投毒本身还伤。
            res = _reload_kernel(app)
            log.warning("已请求 mihomo 重载配置以重建解析：%s", res)
            _STATE["dns_reload"] = res
    elif sys_bad:
        # 现状：系统 DNS（含本机 53 兜底）返回黑洞，但内核 nameserver-policy 挡住了。
        # 只记一条低频告警，避免 60 秒刷一次。
        if _once("dns:sys", 1800):
            first = sys_bad[0]
            _notify(app, "系统 DNS 仍被投毒：" + first["domain"] + " → " + ", ".join(first["bad"])
                     + "（走代理的流量正常，直连会失败）")
            # 日志同样节流，否则 60 秒一条，一天 1440 条把 app.log 刷满
            log.warning("系统 DNS 投毒（内核侧已绕过）：%s", [(d["via"], d["ips"]) for d in sys_bad[:2]])
    else:
        if _ALERTED.pop("dns:kernel", None) or _ALERTED.pop("dns:sys", None):
            _notify(app, "交易域名解析已恢复正常")
            log.info("DNS 解析恢复")
    return dict(_STATE)


def _loop(app: Any) -> None:
    # 启动后先等一轮：内核刚起来时还没测速，立刻探必然误报
    if _STOP.wait(20):
        return
    while True:
        iv = 60
        try:
            st = app.settings
            if bool(st.get("okx_guard", True)):
                tick(app, st)
                iv = max(INTERVAL_MIN, int(st.get("okx_guard_interval", 60) or 60))
            else:
                iv = max(INTERVAL_MIN, 30)
        except Exception as e:  # noqa: BLE001
            log.debug("okxguard 轮次异常: %s", e)
            iv = 60
        if _STOP.wait(iv):
            return


def start(app: Any) -> None:
    """幂等启动。重复调用不会起第二个线程。"""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_loop, args=(app,), name="okxguard", daemon=True)
        _THREAD.start()
    log.info("交易通道守护已启动（REST / WS / 出口 IP / 解析投毒）")


def stop() -> None:
    _STOP.set()


def status() -> dict[str, Any]:
    """给 webui 用的最近一次探测快照（只读拷贝）。"""
    with _LOCK:
        out = dict(_STATE)
        out["dns_detail"] = list(_STATE.get("dns_detail") or [])
        out["dns_bad"] = list(_STATE.get("dns_bad") or [])
        return out


__all__ = ["start", "stop", "status", "tick", "probe_dns", "is_poison", "parse_answer",
           "TRADE_DOMAINS", "SLOW_MS"]
