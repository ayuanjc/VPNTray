# -*- coding: utf-8 -*-
"""netdoctor 的离线单测：不碰网络、不改系统，只验逻辑与格式。

能离线验的：检查项形状、报告渲染、级别汇总、修动作的分发与顺序、
DNS 输出解析、地址段归类。真跑探测留给 _nd.py 这种真机脚本。
"""
import io
import os
import socket
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import netdoctor as nd  # noqa: E402


class FakeApp:
    """够用的 app 桩：fix 里只用到 settings / _set / restart_kernel。"""

    class _S(dict):
        def save(self):
            self["_saved"] = True

    def __init__(self):
        self.settings = FakeApp._S()
        self.set_calls = []
        self.restarted = 0

    def _set(self, **kw):
        self.set_calls.append(kw)
        for k, v in kw.items():
            setattr(self, k, v)

    def restart_kernel(self):
        self.restarted += 1
        return True


class CheckShape(unittest.TestCase):
    def test_fields(self):
        c = nd._ck("x", "标题", "bad", "简述", "详情", fix="flush_dns",
                   fix_title="清缓存", need_admin=True)
        for k in ("id", "title", "level", "brief", "detail", "fix",
                  "fix_title", "need_admin", "data"):
            self.assertIn(k, c)
        self.assertTrue(c["need_admin"])

    def test_kind_of_private(self):
        self.assertIn("家用", nd._kind_cn("192.168.1.9"))
        self.assertIn("172.16/12", nd._kind_cn("172.20.3.4"))
        self.assertIn("10/8", nd._kind_cn("10.1.2.3"))
        self.assertIn("非常见", nd._kind_cn("172.50.86.61"))
        self.assertEqual("本机回环", nd._kind_cn("127.0.0.1"))
        self.assertIn("未知", nd._kind_cn("不是IP"))

    def test_query_bytes(self):
        q = nd._build_query("a.example.com")
        self.assertEqual(len(q[:2]), 2)          # 事务 id
        self.assertEqual(q[2:4], b"\x01\x00")    # RD
        self.assertIn(b"\x01a\x07example\x03com\x00", q)
        self.assertEqual(q[-4:], b"\x00\x01\x00\x01")


class DnsParse(unittest.TestCase):
    def test_ps_json(self):
        fake = ('[{"iface":"以太网","dns":"223.5.5.5,119.29.29.29"},'
                '{"iface":"WLAN","dns":""}]')

        def _run(argv, timeout=6.0):
            if argv and argv[0] == "powershell":
                return {"ok": True, "code": 0, "out": fake, "err": ""}
            return {"ok": True, "code": 0, "out": '接口 "以太网" 的配置\n  静态配置的 DNS 服务器: 223.5.5.5', "err": ""}

        old = nd._run
        nd._run = _run
        try:
            d = nd._dns_servers()
        finally:
            nd._run = old
        self.assertEqual(d["servers"], ["223.5.5.5", "119.29.29.29"])
        self.assertTrue(d["static"])

    def test_ps_broken_falls_back(self):
        netsh = ('接口 "WLAN" 的配置\n'
                 '    静态配置的 DNS 服务器:      223.5.5.5\n'
                 '                              119.29.29.29\n')

        def _run(argv, timeout=6.0):
            if argv and argv[0] == "powershell":
                return {"ok": False, "code": 1, "out": "", "err": "boom"}
            return {"ok": True, "code": 0, "out": netsh, "err": ""}

        old = nd._run
        nd._run = _run
        try:
            d = nd._dns_servers()
        finally:
            nd._run = old
        self.assertEqual(d["servers"], ["223.5.5.5", "119.29.29.29"])


class ProbeAggregate(unittest.TestCase):
    def setUp(self):
        self._old = nd.CHECKS

    def tearDown(self):
        nd.CHECKS = self._old

    def test_levels_and_summary(self):
        nd.CHECKS = (
            lambda app: nd._ck("a", "A", "ok", "好"),
            lambda app: nd._ck("b", "B", "warn", "有点问题"),
            lambda app: nd._ck("c", "C", "bad", "坏了"),
        )
        res = nd.probe(None)
        self.assertEqual(res["level"], "bad")
        self.assertEqual((res["bad"], res["warn"]), (1, 1))
        self.assertIn("坏了", res["summary"])
        self.assertTrue(res["ok"])

    def test_all_ok(self):
        nd.CHECKS = (lambda app: nd._ck("a", "A", "ok", "好"),)
        res = nd.probe(None)
        self.assertEqual(res["level"], "ok")
        self.assertEqual(res["summary"], "全部正常")

    def test_exception_becomes_skip(self):
        def boom(app):
            raise RuntimeError("炸了")

        nd.CHECKS = (boom, lambda app: nd._ck("b", "B", "ok", "好"))
        res = nd.probe(None)
        self.assertEqual(res["checks"][0]["level"], "skip")
        self.assertIn("RuntimeError", res["checks"][0]["brief"])
        self.assertEqual(len(res["checks"]), 2)

    def test_has_fixes_list(self):
        nd.CHECKS = (lambda app: nd._ck("a", "A", "ok", "好"),)
        res = nd.probe(None)
        self.assertEqual([f["action"] for f in res["fixes"]], list(nd.SAFE_FIXES))


class ReportRender(unittest.TestCase):
    def test_empty(self):
        self.assertIn("还没有体检结果", nd.report_text({}))

    def test_with_checks(self):
        res = {"at": "2026-11-13 10:00:00", "level": "bad", "summary": "系统代理指向空端口",
               "elapsed_ms": 1200, "is_admin": False,
               "checks": [{"id": "sysproxy", "title": "系统代理", "level": "bad",
                           "brief": "没人监听", "detail": "ProxyServer: 127.0.0.1:7891",
                           "fix": "fix_proxy", "fix_title": "清空系统代理", "need_admin": False}]}
        txt = nd.report_text(res)
        self.assertIn("[BAD]", txt)
        self.assertIn("系统代理: 没人监听", txt)
        self.assertIn("ProxyServer: 127.0.0.1:7891", txt)
        self.assertIn("清空系统代理", txt)
        self.assertIn("管理员: 否", txt)


class FixDispatch(unittest.TestCase):
    def test_unknown_action(self):
        r = nd.fix(FakeApp(), "nope")
        self.assertFalse(r["ok"])
        self.assertIn("未知修复动作", r["error"])

    def test_flush_dns_runs_command(self):
        seen = []

        def _run(argv, timeout=6.0):
            seen.append(argv)
            return {"ok": True, "code": 0, "out": "已成功刷新 DNS 解析缓存。", "err": ""}

        old = nd._run
        nd._run = _run
        try:
            r = nd.fix(FakeApp(), "flush_dns")
        finally:
            nd._run = old
        self.assertTrue(r["ok"])
        self.assertEqual(seen[0], ["ipconfig", "/flushdns"])
        self.assertEqual(r["title"], "清空 DNS 缓存")

    def test_fix_proxy_turns_off_intent(self):
        app = FakeApp()
        app.settings["system_proxy"] = True
        old = nd.netguard.wipe_system_proxy
        nd.netguard.wipe_system_proxy = lambda: {"ok": True, "cleared": ["ProxyEnable"]}
        try:
            r = nd.fix(app, "fix_proxy")
        finally:
            nd.netguard.wipe_system_proxy = old
        self.assertTrue(r["ok"])
        self.assertFalse(app.settings["system_proxy"])
        self.assertTrue(app.settings.get("_saved"))

    def test_need_admin_reported(self):
        old = nd.netguard.is_admin
        nd.netguard.is_admin = lambda: False
        try:
            r = nd.fix(FakeApp(), "winsock_reset")
        finally:
            nd.netguard.is_admin = old
        self.assertFalse(r["ok"])
        self.assertTrue(r["need_admin"])
        self.assertIn("管理员", r["steps"][0]["out"])

    def test_restart_kernel_calls_app(self):
        app = FakeApp()
        r = nd.fix(app, "restart_kernel")
        self.assertTrue(r["ok"])
        self.assertEqual(app.restarted, 1)


class RepairOrder(unittest.TestCase):
    def test_safe_order(self):
        calls = []
        old_fix, old_probe = nd.fix, nd.probe
        nd.fix = lambda app, action: calls.append(action) or {"ok": True, "action": action}
        nd.probe = lambda app: {"level": "ok"}
        try:
            res = nd.repair(FakeApp())
        finally:
            nd.fix, nd.probe = old_fix, old_probe
        self.assertEqual(calls, list(nd.SAFE_FIXES))
        self.assertTrue(res["ok"])
        self.assertEqual(res["level_after"], "ok")

    def test_pending_admin_collected(self):
        old_fix, old_probe = nd.fix, nd.probe
        nd.fix = lambda app, action: {"ok": False, "action": action, "need_admin": True}
        nd.probe = lambda app: {"level": "warn"}
        try:
            res = nd.repair(FakeApp(), ["fix_dns"])
        finally:
            nd.fix, nd.probe = old_fix, old_probe
        self.assertEqual(res["pending_admin"], ["fix_dns"])

class HostsRepair(unittest.TestCase):
    """hosts 修复器：屏蔽要删、localhost 要留、自定义映射不能动、必须有备份。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="vpntray-hosts-")
        self.path = os.path.join(self.dir, "hosts")
        self.old_env = os.environ.get("VPNTRAY_HOSTS_PATH")
        os.environ["VPNTRAY_HOSTS_PATH"] = self.path
        self.write(
            "# 系统自带的注释\n"
            "127.0.0.1  localhost\r\n"
            "::1  localhost\r\n"
            "0.0.0.0  internetdownloadmanager.com\r\n"
            "127.0.0.1 live.altium.com\r\n"
            "::  telemetry.example.com\r\n"
            "10.0.0.9  nas.lan  nas\r\n"
            "10.0.0.9  nas.lan\r\n"
        )

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("VPNTRAY_HOSTS_PATH", None)
        else:
            os.environ["VPNTRAY_HOSTS_PATH"] = self.old_env
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read(self):
        with open(self.path, "r", encoding="utf-8", newline="") as f:   # 保留 CRLF 原样
            return f.read()

    def test_parse_does_not_flag_localhost(self):
        h = nd._hosts()
        self.assertTrue(h["ok"])
        self.assertIn("internetdownloadmanager.com", h["blocked"])
        self.assertIn("live.altium.com", h["blocked"])
        self.assertNotIn("localhost", h["blocked"])
        self.assertEqual(h["blocked_n"], 3)
        self.assertEqual(h["dup"], 2)          # localhost 两次 + nas.lan 两次 → 两次重复
        self.assertTrue(h["writable"])

    def test_preview_writes_nothing(self):
        before = self.read()
        r = nd.fix_hosts(apply=False, admin=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["dry_run"])
        self.assertEqual(self.read(), before)
        self.assertNotIn("0.0.0.0", r["out"])

    def test_fix_removes_block_keeps_custom_and_localhost(self):
        r = nd.fix_hosts(admin=True)
        self.assertTrue(r["ok"])
        text = self.read()
        self.assertNotIn("internetdownloadmanager.com", text)
        self.assertNotIn("live.altium.com", text)
        self.assertNotIn("telemetry.example.com", text)
        self.assertIn("127.0.0.1  localhost", text)
        self.assertIn("nas.lan", text)
        self.assertIn("10.0.0.9", text)
        self.assertIn("# 系统自带的注释", text)
        self.assertTrue(os.path.exists(r["backup"]))
        self.assertTrue(os.path.exists(self.path + ".vpntray.bak"))
        self.assertIn("\r\n", text)

    def test_fix_dedups_names(self):
        nd.fix_hosts(admin=True)
        text = self.read()
        self.assertEqual(text.count("nas.lan"), 1)

    def test_fix_restores_missing_localhost(self):
        self.write("0.0.0.0 bad.example.com\n")
        r = nd.fix_hosts(admin=True)
        self.assertTrue(r["ok"])
        text = self.read()
        self.assertIn("127.0.0.1  localhost", text)
        self.assertIn("::1  localhost", text)

    def test_fix_requires_admin(self):
        before = self.read()
        r = nd.fix_hosts(admin=False)
        self.assertFalse(r["ok"])
        self.assertTrue(r["need_admin"])
        self.assertEqual(self.read(), before)

    def test_clean_hosts_alias(self):
        old = nd.netguard.is_admin
        nd.netguard.is_admin = lambda: True
        try:
            r = nd._clean_hosts()
        finally:
            nd.netguard.is_admin = old
        self.assertTrue(r["ok"])
        self.assertNotIn("internetdownloadmanager.com", self.read())

    def test_fix_action_dispatch(self):
        old = nd.netguard.is_admin
        nd.netguard.is_admin = lambda: True
        try:
            res = nd.fix(FakeApp(), "fix_hosts")
        finally:
            nd.netguard.is_admin = old
        self.assertTrue(res["ok"])
        self.assertEqual(res["check"]["id"], "hosts")

    def test_check_points_at_fix_hosts(self):
        c = nd.check_one(FakeApp(), "hosts")
        self.assertEqual(c["level"], "warn")
        self.assertEqual(c["fix"], "fix_hosts")
        self.assertTrue(c["need_admin"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
