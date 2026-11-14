"""宿主适配层（vpntray.doctor）的单元测试。

不依赖真的 yuans_nd：往 sys.modules 里塞一个假包，验证的是「宿主这一侧」
的契约——懒启动、密钥只在内存、异常不外抛、提权只给命令。
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import doctor


class FakeSvc:
    last = None
    etw_denied = True

    def __init__(self, data_dir=None, values=None):
        self.data_dir = data_dir
        self.values = values or {}
        self.host = None
        self.started = False
        self.stopped = False
        FakeSvc.last = self

    def set_host(self, h):
        self.host = h

    def start(self):
        self.started = True
        return {"ok": True, "started": True}

    def stop(self):
        self.stopped = True
        return {"ok": True}

    def status(self):
        return {"ok": True, "version": "1.4.0"}

    def diagnose(self, mode="quick", text=""):
        if mode == "boom":
            raise RuntimeError("采集炸了")
        return {"ok": True, "report": {"verdict": "healthy", "mode": mode, "text": text}}

    def detail(self, host, port=443):
        return {"ok": True, "host": host, "port": port}

    def events(self, limit=80, kind=""):
        return {"ok": True, "events": [], "limit": limit, "kind": kind}

    def caps(self):
        return {"admin": False, "estats": True}

    def actions(self):
        return [{"id": "ACT-FLUSH-DNS", "title": "清 DNS 缓存"}]

    def packs(self):
        return {"ok": True, "packs": []}

    def fix(self, act_id, params=None, by="user", why=""):
        return {"ok": True, "run": {"act_id": act_id, "by": by, "why": why, "params": params}}

    def undo(self, run_id, by="user"):
        return {"ok": True, "run_id": run_id, "by": by}

    def clock(self):
        return {"ok": True, "skew_s": 0, "warn": False, "server": "www.msftconnecttest.com"}

    def etw(self, seconds=10.0, top=10):
        if FakeSvc.etw_denied:
            return {"ok": False, "error": {"code": "ERR-ACT-NEED-ADMIN", "msg": "抓 ETW 事件需要管理员"}}
        return {"ok": True, "seconds": seconds, "events": 12, "dns_total": 7, "dns_fail": 0,
                "by_provider": [{"provider": "Microsoft-Windows-DNS-Client", "count": 7}],
                "dns_slowest": [], "dns_failures": []}

    def net(self):
        return {"ok": True, "net": {"v4": {"ok": True, "src": "172.50.86.61", "iface": "WLAN", "ifindex": 18},
                "v6": {"ok": False, "src": ""}, "adapters": 45, "multi": False}}

    def hosts(self):
        return {"ok": True, "path": "C:/Windows/System32/drivers/etc/hosts",
                "entries_total": 12, "blocks_total": 11, "trade": [],
                "blocks": [{"ip": "0.0.0.0", "names": ["internetdownloadmanager.com"]}]}

    def conn(self, host, ports=None, timeout=5.0):
        return {"ok": True, "host": host, "ports": list(ports or [443]), "timeout": timeout,
                "verdict": "blackhole", "hint": "SYN 无应答", "results": [], "summary": {"timeout": 2}}

    def export(self, host="", port=443):
        return {"ok": True, "path": "X:/diag/nd-diag-1.zip", "size": 4096,
                "files": ["README.txt", "manifest.json"], "host": host, "port": port}


class ExportTest(unittest.TestCase):
    def setUp(self):
        _install_fake_nd()
        doctor._SVC = None

    def tearDown(self):
        doctor._SVC = None
        sys.modules.pop("yuans_nd", None)
        sys.modules.pop("yuans_nd.service", None)

    def test_export_passes_host_and_port(self):
        app = types.SimpleNamespace(settings=S({}), restart_kernel=lambda: True)
        out = doctor.export(app, "www.okx.com", port=8443)
        self.assertTrue(out["ok"])
        self.assertEqual(out["path"], "X:/diag/nd-diag-1.zip")
        self.assertEqual(out["host"], "www.okx.com")
        self.assertEqual(out["port"], 8443)

    def test_export_without_nd_reports_missing(self):
        doctor._SVC = None
        sys.modules.pop("yuans_nd", None)
        out = doctor.export(types.SimpleNamespace(settings=S({}), restart_kernel=lambda: True), "")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "ERR-ND-MISSING")



class ClockEtwTest(unittest.TestCase):
    """1.6.0 的两个只读取证入口：时钟与 ETW。"""

    def setUp(self):
        _install_fake_nd()
        doctor._SVC = None
        FakeSvc.etw_denied = True
        self.app = App()

    def tearDown(self):
        doctor._SVC = None
        FakeSvc.etw_denied = True
        for k in ("yuans_nd", "yuans_nd.service", "yuans_nd.elevate"):
            sys.modules.pop(k, None)

    def test_clock_passes_through(self):
        out = doctor.clock(self.app)
        self.assertTrue(out["ok"])
        self.assertEqual(out["skew_s"], 0)

    def test_net_passes_through(self):
        out = doctor.net(self.app)
        self.assertTrue(out["ok"])
        self.assertEqual(out["net"]["v4"]["src"], "172.50.86.61")

    def test_hosts_passes_through(self):
        out = doctor.hosts(self.app)
        self.assertTrue(out["ok"])
        self.assertEqual(out["blocks_total"], 11)

    def test_hosts_without_nd_is_missing(self):
        sys.modules.pop("yuans_nd", None)
        sys.modules.pop("yuans_nd.service", None)
        doctor._SVC = None
        out = doctor.hosts(self.app)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "ERR-ND-MISSING")

    def test_conn_passes_host_ports_timeout(self):
        out = doctor.conn(self.app, "www.okx.com", ports=[443, 22], timeout=3.0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["ports"], [443, 22])
        self.assertEqual(out["verdict"], "blackhole")

    def test_conn_without_nd_is_missing(self):
        sys.modules.pop("yuans_nd", None)
        sys.modules.pop("yuans_nd.service", None)
        doctor._SVC = None
        out = doctor.conn(self.app, "www.okx.com")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "ERR-ND-MISSING")

    def test_etw_denied_attaches_admin_command(self):
        out = doctor.etw(self.app, seconds=10)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "ERR-ACT-NEED-ADMIN")
        # 宿主不自作主张提权，只给一条可复制的命令
        self.assertIn("--elevated", out["elevate"]["display"])
        self.assertIn("etw", out["elevate"]["display"])

    def test_etw_ok_when_admin(self):
        FakeSvc.etw_denied = False
        out = doctor.etw(self.app, seconds=10)
        self.assertTrue(out["ok"])
        self.assertEqual(out["dns_total"], 7)
        self.assertNotIn("elevate", out)


class S(dict):
    def save(self):
        pass


class App:
    def __init__(self, restarted=True):
        self.settings = S({
            "data_dir": os.path.join("E:" + os.sep + "tmp", "doctor-host"),
            "controller_port": 9091,
            "secret": "s3cr3t",
            "mixed_port": 7891,
            "doctor": True,
            "doctor_actions": True,
            "doctor_targets": ["www.okx.com", " ", "okx.com"],
        })
        self.restarted = restarted
        self.calls = 0

    def restart_kernel(self):
        self.calls += 1
        return self.restarted


def _install_fake_nd():
    pkg = types.ModuleType("yuans_nd")
    pkg.__version__ = "9.9.9"
    svc = types.ModuleType("yuans_nd.service")
    svc.Service = FakeSvc
    elev = types.ModuleType("yuans_nd.elevate")

    def build(act_id, params=None, why="", data_dir=""):
        argv = ["python.exe", "-m", "yuans_nd.api.cli", "--json", "fix", act_id]
        return {"available": True, "cmd": argv[0], "args": argv[1:],
                "display": " ".join(argv), "note": "用户点按钮才弹 UAC", "act_id": act_id}

    def build_cli(tail, data_dir=""):
        argv = ["python.exe", "-m", "yuans_nd.api.cli", "--json"] + list(tail) + ["--elevated"]
        return {"available": True, "cmd": argv[0], "args": argv[1:],
                "display": " ".join(argv), "note": "url_open"}

    def run(hint):
        return {"ok": True, "launched": hint.get("act_id")}

    elev.build = build
    elev.build_cli = build_cli
    elev.run = run
    sys.modules["yuans_nd"] = pkg
    sys.modules["yuans_nd.service"] = svc
    sys.modules["yuans_nd.elevate"] = elev


class DoctorTest(unittest.TestCase):
    def setUp(self):
        _install_fake_nd()
        doctor._SVC = None
        doctor._ERR = ""
        self.app = App()

    def tearDown(self):
        doctor._SVC = None
        for k in ("yuans_nd", "yuans_nd.service", "yuans_nd.elevate"):
            sys.modules.pop(k, None)

    # ── 宿主句柄 ──
    def test_host_only_exposes_restart_kernel(self):
        h = doctor._Host(self.app)
        self.assertTrue(h.restart_kernel())
        self.assertEqual(self.app.calls, 1)
        # 没实现的能力就让 yuans_nd 的 getattr 探到 None，由它给明确原因
        self.assertFalse(hasattr(h, "start_dns"))
        self.assertFalse(hasattr(h, "stop_dns"))

    def test_host_swallows_host_exception(self):
        class Boom(App):
            def restart_kernel(self):
                raise RuntimeError("内核炸了")

        self.assertFalse(doctor._Host(Boom()).restart_kernel())

    # ── 配置映射 ──
    def test_values_follow_host_settings(self):
        v = doctor._values(self.app)
        self.assertEqual(v["kernel_port"], 9091)
        self.assertEqual(v["mixed_port"], 7891)
        self.assertEqual(v["kernel_secret_ref"], "s3cr3t")
        # 宿主进程内调用，不再占端口
        self.assertFalse(v["api_enabled"])
        # 空串要去掉，别让空目标占一个探针名额
        self.assertEqual(v["probe_targets"], ["www.okx.com", "okx.com"])

    def test_data_dir_under_host_data_dir(self):
        self.assertTrue(doctor._data_dir(self.app).endswith(os.sep + "doctor"))

    # ── 懒启动 ──
    def test_light_does_not_start_service(self):
        st = doctor.light(self.app)
        self.assertTrue(st["enabled"])
        self.assertFalse(st["started"])
        self.assertIsNone(doctor._SVC)

    def test_status_starts_then_stop_releases(self):
        st = doctor.status(self.app)
        self.assertTrue(st["ok"])
        self.assertEqual(st["host_version"], "9.9.9")
        self.assertTrue(FakeSvc.last.started)
        self.assertIsInstance(FakeSvc.last.host, doctor._Host)
        doctor.stop()
        self.assertTrue(FakeSvc.last.stopped)
        self.assertIsNone(doctor._SVC)

    def test_service_reused_between_calls(self):
        doctor.status(self.app)
        first = doctor._SVC
        doctor.diagnose(self.app, "quick")
        self.assertIs(doctor._SVC, first)

    # ── 异常边界 ──
    def test_diagnose_exception_becomes_error_dict(self):
        out = doctor.diagnose(self.app, "boom")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "ERR-ND-INTERNAL")
        self.assertIn("采集炸了", out["error"]["msg"])

    def test_missing_nd_reports_missing(self):
        sys.modules.pop("yuans_nd", None)
        out = doctor.status(self.app)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "ERR-ND-MISSING")
        ok, why = doctor.available()
        self.assertFalse(ok)

    # ── 动作与提权 ──
    def test_fix_by_user_with_why(self):
        out = doctor.fix(self.app, "ACT-FLUSH-DNS", why="体检建议")
        self.assertTrue(out["ok"])
        self.assertEqual(out["run"]["by"], "user")
        self.assertEqual(out["run"]["why"], "体检建议")

    def test_elevate_returns_command_only(self):
        out = doctor.elevate(self.app, "ACT-FIX-HOSTS")
        self.assertTrue(out["ok"])
        self.assertIn("ACT-FIX-HOSTS", out["display"])
        self.assertIn("用户点按钮才弹 UAC", out["note"])

    def test_elevate_run_reports_cancel(self):
        sys.modules["yuans_nd.elevate"].run = lambda hint: {"ok": False, "cancelled": True,
                                                            "error": "你在 UAC 提示里点了取消"}
        out = doctor.elevate_run(self.app, "ACT-FIX-HOSTS")
        self.assertFalse(out["ok"])
        self.assertTrue(out["result"]["cancelled"])

    def test_actions_list_passes_through(self):
        out = doctor.actions(self.app)
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"][0]["id"], "ACT-FLUSH-DNS")


if __name__ == "__main__":
    unittest.main()
