# -*- coding: utf-8 -*-
"""交易域名 DNS 投毒：配置侧 policy + 体检侧判定。

背景（2026-11-13 真事）：国内 DNS 把 www.okx.com 解析成 CNAME awscn.okpool.top
→ 169.254.0.2，五个交易进程全报 Cannot connect to host www.okx.com:443，
而节点测速 120ms 全绿。doc 里写的 nameserver-policy 当时只人工写进了
data/config.yaml，configgen 里没有 —— 一重启就丢。这组用例锁死两件事：
  1) 生成的配置里必须带 +.okx.com / +.binance.com 的代理 DoH policy；
  2) 体检项 dns_poison 必须把黑洞地址判成 bad，把「上游投毒但本机干净」判成 ok。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import configgen  # noqa: E402
from vpntray import netdoctor as nd  # noqa: E402


def _sub():
    return {
        "proxies": [{"name": "HK-1", "type": "ss", "server": "1.1.1.1", "port": 443,
                     "cipher": "aes-128-gcm", "password": "x"}],
        "proxy-groups": [{"name": "节点选择", "type": "select", "proxies": ["HK-1"]}],
        "rules": ["MATCH,节点选择"],
    }


class TradePolicy(unittest.TestCase):
    def test_domains_and_servers(self):
        self.assertEqual(configgen.TRADE_DNS_DOMAINS, ("+.okx.com", "+.binance.com"))
        for srv in configgen.TRADE_DNS_SERVERS:
            self.assertTrue(srv.startswith("https://"), srv)

    def test_policy_uses_proxy_group(self):
        pol = configgen.trade_dns_policy("节点选择")
        self.assertEqual(sorted(pol), ["+.binance.com", "+.okx.com"])
        for dom, servers in pol.items():
            self.assertEqual(len(servers), len(configgen.TRADE_DNS_SERVERS))
            for srv in servers:
                self.assertTrue(srv.endswith("#节点选择"), srv)

    def test_no_group_means_no_policy(self):
        self.assertEqual(configgen.trade_dns_policy(""), {})
        self.assertEqual(configgen.trade_dns_policy(None), {})

    def test_dns_drops_subscription_policy_and_adds_trade(self):
        sub = _sub()
        sub["dns"] = {"nameserver-policy": {"+.evil.example": ["https://9.9.9.9/dns-query"]}}
        d = configgen._dns(sub, "节点选择")
        pol = d.get("nameserver-policy") or {}
        self.assertNotIn("+.evil.example", pol)
        self.assertIn("+.okx.com", pol)
        self.assertTrue(pol["+.okx.com"][0].endswith("#节点选择"))

    def test_build_output_carries_policy(self):
        cfg = configgen.build(_sub(), {"mixed_port": 7891, "ai_enabled": False,
                                       "scene_auto": True}, {})
        pol = (cfg.get("dns") or {}).get("nameserver-policy") or {}
        self.assertIn("+.okx.com", pol)
        self.assertIn("+.binance.com", pol)


class PoisonJudge(unittest.TestCase):
    def test_blackhole_nets(self):
        for ip in ("169.254.0.2", "127.0.0.1", "0.0.0.0", "224.0.0.1",
                   "255.255.255.255"):
            self.assertTrue(nd._poison_ip(ip), ip)
        for ip in ("104.18.0.1", "8.8.8.8", "1.1.1.1"):
            self.assertFalse(nd._poison_ip(ip), ip)

    def test_known_fingerprints(self):
        self.assertTrue(nd._poison_ip("157.240.16.50", "www.binance.com"))
        self.assertFalse(nd._poison_ip("157.240.16.50", "www.okx.com"))
        self.assertEqual(nd._poison_cname("www.okx.com", ["awscn.okpool.top"]),
                         "awscn.okpool.top")
        self.assertEqual(nd._poison_cname("www.okx.com", ["okx.com"]), "")

    def _run(self, mapping, servers=("223.5.5.5",)):
        old_srv, old_ask = nd._dns_servers, nd._ask_full
        nd._dns_servers = lambda: {"servers": list(servers), "static": True,
                                   "per_if": [], "error": ""}
        nd._ask_full = lambda server, name, timeout=2.0: dict(mapping[(server, name)])
        try:
            return nd._chk_dns_poison(None)
        finally:
            nd._dns_servers, nd._ask_full = old_srv, old_ask

    @staticmethod
    def _ok(ips, cnames=()):
        return {"ok": True, "ms": 5, "ips": list(ips), "cnames": list(cnames),
                "rcode": 0, "error": ""}

    def test_effective_path_poisoned_is_bad(self):
        r = self._run({("223.5.5.5", "www.okx.com"): self._ok(["169.254.0.2"],
                                                             ["awscn.okpool.top"]),
                       ("223.5.5.5", "www.binance.com"): self._ok(["104.16.1.1"])})
        self.assertEqual(r["level"], "bad")
        self.assertIn("169.254.0.2", r["brief"])
        self.assertEqual(r["fix"], "flush_dns")

    def test_upstream_poison_but_local_clean_is_ok(self):
        r = self._run({
            ("127.0.0.1", "www.okx.com"): self._ok(["104.18.1.1"]),
            ("127.0.0.1", "www.binance.com"): self._ok(["104.16.1.1"]),
            ("223.5.5.5", "www.okx.com"): self._ok(["169.254.0.2"]),
            ("223.5.5.5", "www.binance.com"): self._ok(["157.240.16.50"]),
        }, servers=("127.0.0.1",))
        self.assertEqual(r["level"], "ok")
        self.assertIn("上游", r["brief"])

    def test_all_queries_failing_is_skip(self):
        r = self._run({("223.5.5.5", "www.okx.com"): {"ok": False, "ips": [],
                                                      "cnames": [], "error": "timeout"},
                       ("223.5.5.5", "www.binance.com"): {"ok": False, "ips": [],
                                                          "cnames": [], "error": "timeout"}})
        self.assertEqual(r["level"], "skip")

    def test_no_a_record_is_warn(self):
        r = self._run({("223.5.5.5", "www.okx.com"): self._ok([], ["a.example"]),
                       ("223.5.5.5", "www.binance.com"): self._ok(["104.16.1.1"])})
        self.assertEqual(r["level"], "warn")

    def test_policy_present_downgrades_to_warn(self):
        import tempfile
        from vpntray import paths as P
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, "config.yaml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write("dns:\n  nameserver-policy:\n    +.okx.com:\n    - https://1.1.1.1/dns-query#节点选择\n")
        old = P.CONFIG
        P.CONFIG = type(old)(cfg)
        try:
            r = self._run({("223.5.5.5", "www.okx.com"): self._ok(["169.254.0.2"],
                                                                 ["awscn.okpool.top"]),
                           ("223.5.5.5", "www.binance.com"): self._ok(["104.16.1.1"])})
        finally:
            P.CONFIG = old
        self.assertEqual(r["level"], "warn")
        self.assertIn("已走代理 DoH", r["brief"])
        self.assertEqual(r["fix"], "start_dns")

    def test_check_registered(self):
        # CHECKS 里放的是函数对象，顺序即体检顺序
        names = [getattr(fn, "__name__", "") for fn in nd.CHECKS]
        self.assertIn("_chk_dns_poison", names)
        self.assertEqual(len(nd.CHECKS), 13)
        # 投毒项必须排在本机/公共 DNS 之后、系统代理之前（先看解析，再看代理）
        self.assertLess(names.index("_chk_dns_public"), names.index("_chk_dns_poison"))
        self.assertLess(names.index("_chk_dns_poison"), names.index("_chk_sysproxy"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
