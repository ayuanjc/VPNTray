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
from vpntray import dnsguard  # noqa: E402


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


class IfaceNameParse(unittest.TestCase):
    """网卡名解析：netsh 的列布局逐语言、逐版本变，绝不能按字符位切。"""

    EN = (
        "Admin State    State          Type             Interface Name\n"
        "-------------------------------------------------------------------------\n"
        "Enabled        Connected      Dedicated        WLAN\n"
        "Enabled        Disconnected   Dedicated        Ethernet 2\n"
    )
    ZH = (
        "管理状态       状态           类型             接口名称\n"
        "-------------------------------------------------------------------------\n"
        "已启用         已连接         专用             WLAN\n"
        "已启用         已断开         专用             以太网 2\n"
    )

    def test_netsh_english(self):
        self.assertEqual(nd._parse_netsh_interfaces(self.EN), ["WLAN"])

    def test_netsh_chinese(self):
        self.assertEqual(nd._parse_netsh_interfaces(self.ZH), ["WLAN"])

    def test_active_ifaces_prefers_powershell(self):
        old = nd._run
        nd._run = lambda argv, timeout=6.0: {"ok": True, "code": 0,
                                             "out": "WLAN\r\n以太网\r\n", "err": ""}
        try:
            self.assertEqual(nd._active_ifaces(), ["WLAN", "以太网"])
        finally:
            nd._run = old

    def test_active_ifaces_falls_back_to_netsh(self):
        old = nd._run

        def _run(argv, timeout=6.0):
            if argv and argv[0] == "powershell":
                return {"ok": False, "code": 1, "out": "", "err": "拒绝访问"}
            return {"ok": True, "code": 0, "out": self.ZH, "err": ""}

        nd._run = _run
        try:
            self.assertEqual(nd._active_ifaces(), ["WLAN"])
        finally:
            nd._run = old


class HostsGbk(unittest.TestCase):
    """GBK 的 hosts 修完必须还是 GBK：按 UTF-8 写回会把中文注释永久变成 U+FFFD。"""

    BODY = "# 由维护工具生成\r\n0.0.0.0 bad.example.com\r\n10.0.0.9 nas.lan\r\n"

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="vpntray-gbk-")
        self.path = os.path.join(self.dir, "hosts")
        self.old_env = os.environ.get("VPNTRAY_HOSTS_PATH")
        os.environ["VPNTRAY_HOSTS_PATH"] = self.path
        with open(self.path, "wb") as f:
            f.write(HostsGbk.BODY.encode("gbk"))

    def tearDown(self):
        if self.old_env is None:
            os.environ.pop("VPNTRAY_HOSTS_PATH", None)
        else:
            os.environ["VPNTRAY_HOSTS_PATH"] = self.old_env
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_read_keeps_chinese(self):
        h = nd._hosts()
        self.assertTrue(h["ok"])
        self.assertIn("bad.example.com", h["blocked"])
        self.assertIn("由维护工具生成", h["raw"])
        self.assertTrue(h["writable"])

    def test_fix_writes_back_gbk(self):
        r = nd.fix_hosts(admin=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["encoding"], "gbk")
        with open(self.path, "rb") as f:
            raw = f.read()
        text = raw.decode("gbk")               # 还能按 GBK 解 → 没被改成 UTF-8
        self.assertIn("由维护工具生成", text)
        self.assertNotIn(b"\xef\xbf\xbd", raw)  # 没有任何替换字符
        self.assertNotIn("bad.example.com", text)
        self.assertIn("nas.lan", text)
        self.assertTrue(os.stat(self.path).st_mode & 0o200)   # 权限位还在

    def test_preview_does_not_touch_file(self):
        with open(self.path, "rb") as f:
            before = f.read()
        r = nd.fix_hosts(apply=False, admin=True)
        self.assertTrue(r["ok"])
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)


class PortsFromSettings(unittest.TestCase):
    """内核/解析端口必须跟着 settings 走，否则改过端口的用户会被误报「内核没起来」。"""

    def test_ports_follow_settings(self):
        app = FakeApp()
        app.settings["mixed_port"] = 7895
        app.settings["controller_port"] = "9095"
        app.settings["dns_port"] = 5353
        cfg = nd._ports(app)
        self.assertEqual((cfg["mixed"], cfg["controller"], cfg["dns"]), (7895, 9095, 5353))

    def test_ports_fallback_on_garbage(self):
        app = FakeApp()
        app.settings["mixed_port"] = None
        app.settings["controller_port"] = 999999
        cfg = nd._ports(app)
        self.assertEqual((cfg["mixed"], cfg["controller"]), (7891, 9091))
        self.assertEqual(nd._ports(None)["mixed"], 7891)      # 没有 app 也要能查


class GatewayPingSkip(unittest.TestCase):
    """ping 跑不起来（被拦 / 缺命令）只能说「无法判定」，不能说网关不通。"""

    def test_ping_unavailable_is_skip(self):
        old_ident, old_run = nd.hotspot.identity, nd._run
        nd.hotspot.identity = lambda *a, **k: {"gw": "172.50.86.1", "ip": "172.50.86.61"}
        nd._run = lambda argv, timeout=6.0: {"ok": False, "code": -1, "out": "",
                                             "err": "拒绝访问"}
        try:
            c = nd._chk_gateway(None)
        finally:
            nd.hotspot.identity, nd._run = old_ident, old_run
        self.assertEqual(c["level"], "skip")
        self.assertEqual(c["id"], "gateway")
        self.assertIn("无法执行", c["brief"])

    def test_ping_total_loss_still_bad(self):
        old_ident, old_run = nd.hotspot.identity, nd._run
        nd.hotspot.identity = lambda *a, **k: {"gw": "172.50.86.1"}
        nd._run = lambda argv, timeout=6.0: {
            "ok": False, "code": 0, "err": "",
            "out": "正在 Ping 172.50.86.1 具有 32 字节的数据:\r\n请求超时。\r\n\r\n"
                   "数据包: 已发送 = 3，已接收 = 0，丢失 = 3 (100% 丢失)，\r\n"}
        try:
            c = nd._chk_gateway(None)
        finally:
            nd.hotspot.identity, nd._run = old_ident, old_run
        self.assertEqual(c["level"], "bad")
        self.assertTrue(c["data"]["ran"])


class IdnaProbe(unittest.TestCase):
    """中文 / 超长域名必须走 IDNA，否则 getaddrinfo 抛 UnicodeError 把整轮体检炸掉。"""

    def test_idna_encodes_chinese(self):
        self.assertEqual(nd._idna("www.baidu.com"), "www.baidu.com")
        self.assertTrue(nd._idna("中文域名.测试").isascii())
        self.assertTrue(nd._idna("a" * 300 + ".com"))       # 超长标签不抛异常

    def test_dns_public_uses_idna(self):
        seen = []
        old = (nd.PROBE_NAMES, nd._gai, nd._ask)

        def _gai(name, timeout=3.0):
            seen.append(name)
            return {"ms": None, "error": "gaierror"}

        nd.PROBE_NAMES = (("中文域名.测试", "国内"),)
        nd._gai = _gai
        nd._ask = lambda srv, name, timeout=1.6: {"ok": False, "ms": 0,
                                                  "error": "无应答", "answers": 0}
        try:
            c = nd._chk_dns_public(None)
        finally:
            nd.PROBE_NAMES, nd._gai, nd._ask = old
        self.assertEqual(c["level"], "bad")
        self.assertTrue(seen and seen[0].isascii())


class SysproxyBackupFidelity(unittest.TestCase):
    """清理别家代理不能吃掉用户的原始设置：备份是退出时还原的唯一依据。"""

    def setUp(self):
        import pathlib
        import tempfile
        from vpntray import sysproxy
        self.sp = sysproxy
        self.dir = tempfile.mkdtemp(prefix="vpntray-proxy-")
        base = pathlib.Path(self.dir)
        self._paths = (sysproxy.BACKUP, sysproxy.LEGACY_BACKUP, sysproxy.DIRECT_MARK)
        sysproxy.BACKUP = base / "backup.json"
        sysproxy.LEGACY_BACKUP = base / "legacy.json"
        sysproxy.DIRECT_MARK = base / "direct.json"
        self.reg = {"ProxyEnable": 1, "ProxyServer": "10.1.2.3:8080",
                    "ProxyOverride": "local",
                    "AutoConfigURL": "http://pac.example/proxy.pac"}
        self.writes = []
        self._orig = (sysproxy._read, sysproxy._write, sysproxy._delete,
                      sysproxy._internet_set_option)
        sysproxy._read = lambda name: self.reg.get(name)

        def _write(name, value, kind=None, tries=4):
            self.writes.append((name, value))
            self.reg[name] = value

        def _delete(name):
            self.writes.append((name, None))
            self.reg.pop(name, None)

        sysproxy._write = _write
        sysproxy._delete = _delete
        sysproxy._internet_set_option = lambda: None

    def tearDown(self):
        (self.sp._read, self.sp._write, self.sp._delete,
         self.sp._internet_set_option) = self._orig
        (self.sp.BACKUP, self.sp.LEGACY_BACKUP, self.sp.DIRECT_MARK) = self._paths
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_wipe_keeps_original_backup(self):
        original = dict(self.reg)
        r = nd.netguard.wipe_system_proxy()
        self.assertTrue(r["ok"])
        bak = self.sp._load_backup() or {}
        self.assertEqual(bak.get("ProxyServer"), original["ProxyServer"])
        self.assertEqual(bak.get("ProxyOverride"), original["ProxyOverride"])
        self.assertEqual(bak.get("AutoConfigURL"), original["AutoConfigURL"])
        self.assertEqual(int(bak.get("ProxyEnable") or 0), 1)
        nd.netguard.wipe_system_proxy()          # 再清一次：备份仍然不许变
        self.assertEqual((self.sp._load_backup() or {}).get("ProxyServer"),
                         original["ProxyServer"])

    def test_disable_after_wipe_restores_original_values(self):
        original = dict(self.reg)
        nd.netguard.wipe_system_proxy()
        self.sp.disable()
        self.assertIn(("ProxyServer", original["ProxyServer"]), self.writes)
        self.assertIn(("AutoConfigURL", original["AutoConfigURL"]), self.writes)
        self.assertIn(("ProxyOverride", original["ProxyOverride"]), self.writes)
        # 用户此前点过「一键清理」，最终状态保持直连，不把死代理装回去
        self.assertFalse(self.reg.get("ProxyEnable"))


class DnsPortExclusive(unittest.TestCase):
    """53 必须独占：只设 SO_REUSEADDR 时别的进程能共享端口，running 就是谎报。"""

    def test_exclusive_bind_rejects_second(self):
        from vpntray import dnsguard
        if os.name != "nt":
            self.skipTest("SO_EXCLUSIVEADDRUSE 只在 Windows 上存在")
        a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            dnsguard._bind_exclusive(a)
            a.bind(("127.0.0.1", 0))
            port = a.getsockname()[1]
            dnsguard._bind_exclusive(b)
            with self.assertRaises(OSError):
                b.bind(("127.0.0.1", port))
        finally:
            b.close()
            a.close()

    def test_port_busy_sees_holder(self):
        from vpntray import dnsguard
        if os.name != "nt":
            self.skipTest("独占语义只在 Windows 上可测")
        hold = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            hold.bind(("127.0.0.1", 0))
            self.assertTrue(dnsguard.port_busy(hold.getsockname()[1]))
        finally:
            hold.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
