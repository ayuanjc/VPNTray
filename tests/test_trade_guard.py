# -*- coding: utf-8 -*-
"""交易通道守护（okxguard）+ 兜底 DoH + 内核活体检查。

背景（2026-11-13 真事）：系统 DNS 把 www.okx.com 解成 CNAME awscn.okpool.top
→ 169.254.0.2，五个交易进程全报 Cannot connect to host ... 而节点测速全绿。
当时的监控只看 REST 延迟和 WS 连通 —— 这两条都走代理 CONNECT，由 mihomo 自己解析，
所以「代理通、直连死」的状态完全看不见。这组用例锁死：
  1) 黑洞地址必须被 is_poison 认出来；
  2) probe_dns 能区分「内核侧脏」「只有系统侧脏」；
  3) tick 在内核侧脏时会请求重载，只在系统侧脏时只告警不动机器；
  4) 兜底 DNS 对交易域名改走 DoH，不再转发投毒答案；
  5) 内核进程活着但 API 连续无应答时按崩溃处理。
"""
from __future__ import annotations

import os
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import dnsguard  # noqa: E402
from vpntray import kernel as K  # noqa: E402
from vpntray import okxguard as G  # noqa: E402


def _name(text: str) -> bytes:
    out = b""
    for part in text.rstrip(".").split("."):
        raw = part.encode("ascii")
        out += bytes([len(raw)]) + raw
    return out + bytes([0])


def _resp(qid: int, qname="www.okx.com", ip="104.18.43.174", cname="", ttl=300) -> bytes:
    """组一条带 QR=1 的应答：可选 CNAME + 一条 A。"""
    q = _name(qname) + struct.pack("!HH", 1, 1)
    an = 1 + (1 if cname else 0)
    out = struct.pack("!HHHHHH", qid, 0x8180, 1, an, 0, 0) + q
    if cname:
        rd = _name(cname)
        out += _name(qname) + struct.pack("!HHIH", 5, 1, ttl, len(rd)) + rd
    rd = bytes(int(x) for x in ip.split("."))
    out += _name(cname or qname) + struct.pack("!HHIH", 1, 1, ttl, 4) + rd
    return out


def _reset():
    G._STATE.update({"checked_at": 0.0, "ip": "", "ip_from": "", "country": "",
                     "rest_ms": 0, "rest_cold_ms": 0, "ws_ok": None, "dns_ok": None,
                     "dns_sys_ok": None, "dns_detail": [], "dns_bad": [], "error": ""})
    G._ALERTED.clear()
    G._SYS_CACHE.update({"ts": 0.0, "servers": ("223.5.5.5",)})


class Poison(unittest.TestCase):
    def test_blackhole_and_private(self):
        for ip in ("169.254.0.2", "0.0.0.0", "127.0.0.1", "198.18.0.5",
                   "100.64.1.1", "224.0.0.1", "255.255.255.255"):
            self.assertTrue(G.is_poison(ip), ip)

    def test_real_addresses_pass(self):
        for ip in ("104.18.43.174", "172.64.144.82", "8.8.8.8", "", "不是IP"):
            self.assertFalse(G.is_poison(ip), ip)


class Packet(unittest.TestCase):
    def test_query_roundtrip(self):
        txid, pkt = G._build_query("www.okx.com")
        self.assertEqual(len(pkt[:2]), 2)
        self.assertEqual(struct.unpack(">H", pkt[:2])[0], txid)
        self.assertEqual(dnsguard._qname(pkt), "www.okx.com")

    def test_parse_answer_cname_and_a(self):
        resp = _resp(0x1234, cname="awscn.okpool.top", ip="169.254.0.2")
        ips, cnames, rcode = G.parse_answer(resp)
        self.assertEqual(ips, ["169.254.0.2"])
        self.assertEqual(cnames, ["awscn.okpool.top"])
        self.assertEqual(rcode, 0)

    def test_parse_answer_garbage(self):
        self.assertEqual(G.parse_answer(b"")[0], [])


class _Api:
    """内核 /dns/query 桩：所有域名返回同一批 IP，并记录问过哪些域名。"""

    def __init__(self, ips):
        self.ips = ips
        self.paths = []

    def _req(self, method, path, timeout=4.0):
        self.paths.append(path)
        return {"Answer": [{"type": 1, "data": ip} for ip in self.ips]}


class _App:
    def __init__(self, ips):
        self.api = _Api(ips)


class ProbeDns(unittest.TestCase):
    """probe_dns 必须分开报告「内核侧」和「系统侧」。"""

    def setUp(self):
        _reset()

    def tearDown(self):
        _reset()

    def _run(self, kernel_ips, sys_ips):
        app = _App(kernel_ips)
        old_sys, old_ask = G._system_servers, G._ask_udp
        calls = []

        def fake_ask(server, name, timeout=2.0):
            calls.append((server, name))
            return (list(sys_ips), ["awscn.okpool.top"] if sys_ips else [], 0, "")

        G._system_servers = lambda ttl=600.0: ("223.5.5.5",)
        G._ask_udp = fake_ask
        try:
            res = G.probe_dns(app)
        finally:
            G._system_servers, G._ask_udp = old_sys, old_ask
        return res, calls, app

    def test_kernel_clean_system_poisoned(self):
        res, calls, app = self._run(["104.18.43.174"], ["169.254.0.2"])
        self.assertIs(res["kernel_ok"], True)
        self.assertIs(res["sys_ok"], False)
        self.assertIn("www.okx.com", res["bad"])
        # 系统侧只查代表域名，不逐个查 —— 否则一轮 4 域名 × 3 服务器 = 12 次超时
        self.assertTrue(calls)
        self.assertEqual({c[1] for c in calls}, {"www.okx.com"})
        # 内核侧四个域名都问过
        self.assertEqual(len(app.api.paths), len(G.TRADE_DOMAINS))

    def test_kernel_poisoned(self):
        res, _calls, _app = self._run(["169.254.0.2"], ["104.18.43.174"])
        self.assertIs(res["kernel_ok"], False)
        self.assertIs(res["sys_ok"], True)

    def test_all_clean(self):
        res, _c, _a = self._run(["104.18.43.174"], ["104.18.43.174"])
        self.assertIs(res["kernel_ok"], True)
        self.assertIs(res["sys_ok"], True)
        self.assertEqual(res["bad"], [])


class Tick(unittest.TestCase):
    def setUp(self):
        _reset()
        self.sent = []
        self.reloaded = []
        self.orig = (G.probe_ip, G.probe_rest, G.probe_ws, G.probe_dns,
                     G._reload_kernel, G._notify)

    def tearDown(self):
        (G.probe_ip, G.probe_rest, G.probe_ws, G.probe_dns,
         G._reload_kernel, G._notify) = self.orig
        _reset()

    def _patch(self, dns):
        G.probe_ip = lambda port: ("1.2.3.4", "日本", "")
        G.probe_rest = lambda port: (120, 900, "")
        G.probe_ws = lambda port: (True, "")
        G.probe_dns = lambda app: dns
        G._reload_kernel = lambda app: (self.reloaded.append(1), "ok")[1]
        G._notify = lambda app, text, level="warning": self.sent.append(text)

    def _dns(self, kernel_ok, sys_ok, bad):
        return {"kernel_ok": kernel_ok, "sys_ok": sys_ok, "bad": bad, "detail": [
            {"domain": "www.okx.com", "via": "kernel",
             "ips": ["169.254.0.2"] if kernel_ok is False else ["104.18.43.174"],
             "bad": ["169.254.0.2"] if kernel_ok is False else [], "err": ""},
            {"domain": "www.okx.com", "via": "223.5.5.5",
             "ips": ["169.254.0.2"] if sys_ok is False else ["104.18.43.174"],
             "cnames": ["awscn.okpool.top"] if sys_ok is False else [],
             "bad": ["169.254.0.2"] if sys_ok is False else [], "err": ""},
        ]}

    def test_kernel_poison_triggers_reload(self):
        self._patch(self._dns(False, False, ["www.okx.com"]))
        st = {"mixed_port": 7891}
        G.tick(None, st)
        self.assertTrue(self.reloaded, "内核侧脏必须请求重载")
        self.assertTrue(any("穿透内核" in t for t in self.sent), self.sent)
        self.assertIs(G.status()["dns_ok"], False)

    def test_system_poison_only_warns(self):
        self._patch(self._dns(True, False, ["www.okx.com"]))
        G.tick(None, {"mixed_port": 7891})
        self.assertEqual(self.reloaded, [], "系统侧脏不该重载内核（本来就没走它）")
        self.assertTrue(any("系统 DNS 仍被投毒" in t for t in self.sent), self.sent)

    def test_clean_silent(self):
        self._patch(self._dns(True, True, []))
        G.tick(None, {"mixed_port": 7891})
        self.assertEqual(self.sent, [])
        self.assertEqual(self.reloaded, [])

    def test_rest_slow_warns_once(self):
        self._patch(self._dns(True, True, []))
        G.probe_rest = lambda port: (1500, 900, "")
        G.tick(None, {"mixed_port": 7891})
        G.tick(None, {"mixed_port": 7891})
        slow = [t for t in self.sent if "REST 慢" in t]
        self.assertEqual(len(slow), 1, "同一条告警 300 秒内只发一次")

    def test_status_is_copy(self):
        snap = G.status()
        snap["dns_bad"].append("x")
        self.assertEqual(G.status()["dns_bad"], [])


class Edge(unittest.TestCase):
    def setUp(self):
        G._ALERTED.clear()

    def test_once_gate(self):
        self.assertTrue(G._once("k", 600))
        self.assertFalse(G._once("k", 600))
        self.assertTrue(G._once("other", 600))


class DohFallback(unittest.TestCase):
    """兜底 DNS：交易域名走 DoH，其它域名照旧转发。"""

    def setUp(self):
        self.old_port = dnsguard._DOH_PORT
        self.old_doh = dnsguard._doh
        self.old_udp = dnsguard.DnsGuard._query_udp
        self.got = []

    def tearDown(self):
        dnsguard._DOH_PORT = self.old_port
        dnsguard._doh = self.old_doh
        dnsguard.DnsGuard._query_udp = self.old_udp

    def _guard(self):
        g = dnsguard.DnsGuard.__new__(dnsguard.DnsGuard)
        g._lock = threading.Lock()
        g._cache = {}
        g.upstreams = ("223.5.5.5",)
        g.hits = 0
        g.queries = 0
        g.errors = 0
        g.upstream_ok = 0
        g.last_error = ""
        return g

    def test_domains(self):
        self.assertTrue(dnsguard._doh_wanted(dnsguard._build_query("www.okx.com")[1]))
        self.assertTrue(dnsguard._doh_wanted(dnsguard._build_query("app.binance.com")[1]))
        self.assertFalse(dnsguard._doh_wanted(dnsguard._build_query("www.baidu.com")[1]))

    def test_no_proxy_means_no_doh(self):
        dnsguard.set_doh_proxy(0)
        self.assertIsNone(dnsguard._doh(dnsguard._build_query("www.okx.com")[1]))

    def test_resolve_uses_doh(self):
        dnsguard.set_doh_proxy(7891)
        qid, pkt = dnsguard._build_query("www.okx.com")
        good = _resp(qid, ip="104.18.43.174")

        def fake_doh(data):
            self.got.append("doh")
            return good

        def boom(self, host, data):
            raise AssertionError("交易域名不该再转发给国内上游")

        dnsguard._doh = fake_doh
        dnsguard.DnsGuard._query_udp = boom
        g = self._guard()
        out = g._resolve(pkt)
        self.assertEqual(out, good)
        self.assertEqual(self.got, ["doh"])
        self.assertIn(dnsguard._cache_key(pkt), g._cache)

    def test_falls_back_when_doh_fails(self):
        dnsguard.set_doh_proxy(7891)
        qid, pkt = dnsguard._build_query("www.okx.com")
        upstream = _resp(qid, ip="8.8.4.4")
        dnsguard._doh = lambda data: None
        dnsguard.DnsGuard._query_udp = lambda self, host, data: upstream
        out = self._guard()._resolve(pkt)
        self.assertEqual(out, upstream)

    def test_poisoned_detection(self):
        qid = 0x1234
        self.assertTrue(dnsguard._poisoned(_resp(qid, ip="169.254.0.2")))
        self.assertTrue(dnsguard._poisoned(_resp(qid, ip="198.18.0.5")))
        self.assertTrue(dnsguard._poisoned(_resp(qid, ip="0.0.0.0")))
        self.assertFalse(dnsguard._poisoned(_resp(qid, ip="104.18.43.174")))
        self.assertFalse(dnsguard._poisoned(b""))

    def test_doh_fail_upstream_poison_is_rejected(self):
        """DoH 拿不到时，上游的投毒答案既不能回给客户端，也不能进缓存。"""
        dnsguard.set_doh_proxy(7891)
        qid, pkt = dnsguard._build_query("www.okx.com")
        poison = _resp(qid, cname="awscn.okpool.top", ip="169.254.0.2")
        dnsguard._doh = lambda data: None
        dnsguard.DnsGuard._query_udp = lambda self, host, data: poison
        g = self._guard()
        self.assertIsNone(g._resolve(pkt))
        self.assertNotIn(dnsguard._cache_key(pkt), g._cache)
        self.assertEqual(g.upstream_ok, 0)

    def test_non_trade_domain_untouched(self):
        """非交易域名即使上游给了黑洞地址也照原样回（不替用户做决定）。"""
        dnsguard.set_doh_proxy(7891)
        qid, pkt = dnsguard._build_query("www.example.com")
        upstream = _resp(qid, qname="www.example.com", ip="169.254.0.2")
        dnsguard._doh = lambda data: None
        dnsguard.DnsGuard._query_udp = lambda self, host, data: upstream
        self.assertEqual(self._guard()._resolve(pkt), upstream)


class _Ev:
    """假 Event：wait() 前 stop_after 次返回 False，之后返回 True。"""

    def __init__(self, stop_after=20):
        self.n = 0
        self.stop_after = stop_after

    def wait(self, timeout):
        self.n += 1
        return self.n > self.stop_after

    def is_set(self):
        return self.n > self.stop_after


class _Proc:
    pid = 4242

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode


class KernelLiveness(unittest.TestCase):
    """进程活着但 API 不应答：连续 3 次后按崩溃处理，不能一直挂着死代理。"""

    def _run(self, alive: bool):
        k = K.Kernel.__new__(K.Kernel)
        k.proc = _Proc()
        k._stop = _Ev(40)
        k._restarts = 0
        k.last_error = ""
        k._lock = threading.Lock()
        k.api = type("A", (), {"alive": lambda self, timeout=1.0: alive})()
        killed = []
        proc = k.proc
        k._terminate = lambda wait=3.0: (killed.append(1), setattr(proc, "returncode", -9))
        k.start = lambda *a, **kw: False
        k._watchdog()
        return killed, proc

    def test_stuck_kernel_restarted(self):
        killed, proc = self._run(False)
        self.assertEqual(len(killed), 1, "僵死内核必须被终止并进入重启流程")
        self.assertEqual(proc.returncode, -9, "终止后必须落下真实退出码")

    def test_healthy_kernel_untouched(self):
        killed, _k = self._run(True)
        self.assertEqual(killed, [])

    def test_healthy_kernel_not_restarted(self):
        """回归：健康内核每个 tick 都不能走重启流程（曾因少了 continue 每 2 秒重启一次）。"""
        k = K.Kernel.__new__(K.Kernel)
        k.proc = _Proc()
        k._stop = _Ev(12)
        k._restarts = 0
        k.last_error = ""
        k._lock = threading.Lock()
        k.api = type("A", (), {"alive": lambda self, timeout=1.0: True})()
        calls = []
        k._terminate = lambda wait=3.0: calls.append("kill")
        k.start = lambda *a, **kw: calls.append("start") or True
        k._watchdog()
        self.assertEqual(calls, [], "API 正常时不该 kill、不该重启")
        self.assertEqual(k._restarts, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
