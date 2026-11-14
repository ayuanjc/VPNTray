"""WebUI 的诊断路由接线测试。

不起 HTTP 服务：只拿 _routes_scene() 的字典，逐个调用 handler，验证
「前端能调的入口都在」以及参数校验、错误码原样透传。
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import doctor, webui

ROUTES = ("/api/doctor/status", "/api/doctor/diagnose", "/api/doctor/detail",
          "/api/doctor/events", "/api/doctor/actions", "/api/doctor/fix",
          "/api/doctor/undo")


class S(dict):
    def save(self):
        pass


class App:
    def __init__(self):
        self.settings = S({"data_dir": os.path.join("E:" + os.sep + "tmp", "webui-doctor"),
                           "controller_port": 9091, "secret": "k", "mixed_port": 7891,
                           "doctor": True, "doctor_actions": True})

    def restart_kernel(self):
        return True


def _install_fake_nd():
    pkg = types.ModuleType("yuans_nd")
    pkg.__version__ = "9.9.9"
    svc = types.ModuleType("yuans_nd.service")

    class Fake:
        def __init__(self, data_dir=None, values=None):
            pass

        def set_host(self, h):
            pass

        def start(self):
            return {"ok": True}

        def stop(self):
            return {"ok": True}

        def status(self):
            return {"ok": True, "sources": {"kernel": "ok"}}

        def diagnose(self, mode="quick", text=""):
            return {"ok": True, "report": {"verdict": "degraded", "headline": "系统解析被投毒"}}

        def detail(self, host, port=443):
            return {"ok": True, "host": host, "port": port, "dns": [], "trace": {}}

        def events(self, limit=80, kind=""):
            return {"ok": True, "events": [], "total": 0}

        def caps(self):
            return {"admin": False}

        def actions(self):
            return [{"id": "ACT-FLUSH-DNS", "title": "清 DNS 缓存"}]

        def packs(self):
            return {"ok": True, "packs": []}

        def fix(self, act_id, params=None, by="user", why=""):
            return {"ok": True, "run": {"act_id": act_id, "run_id": "a-1", "by": by}}

        def undo(self, run_id, by="user"):
            return {"ok": True, "run_id": run_id}

    svc.Service = Fake
    sys.modules["yuans_nd"] = pkg
    sys.modules["yuans_nd.service"] = svc


class RouteTest(unittest.TestCase):
    def setUp(self):
        _install_fake_nd()
        doctor._SVC = None
        self.app = App()
        self.ui = webui.WebUI(self.app, lambda: None, port=0)
        self.routes = self.ui._routes_scene()

    def tearDown(self):
        doctor._SVC = None
        for k in ("yuans_nd", "yuans_nd.service"):
            sys.modules.pop(k, None)

    def test_all_doctor_routes_registered(self):
        for r in ROUTES:
            self.assertIn(r, self.routes, r)

    def test_status_returns_state(self):
        out = self.routes["/api/doctor/status"]({})
        self.assertTrue(out["ok"])
        self.assertEqual(out["doctor"]["host_version"], "9.9.9")

    def test_diagnose_rejects_unknown_mode(self):
        out = self.routes["/api/doctor/diagnose"]({"mode": "weird"})
        self.assertFalse(out["ok"])
        self.assertIn("quick", out["error"])

    def test_diagnose_passes_mode(self):
        out = self.routes["/api/doctor/diagnose"]({"mode": "paste", "text": "炸了"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["report"]["verdict"], "degraded")

    def test_detail_requires_host(self):
        out = self.routes["/api/doctor/detail"]({})
        self.assertFalse(out["ok"])
        self.assertIn("主机名", out["error"])

    def test_detail_default_port(self):
        out = self.routes["/api/doctor/detail"]({"host": "www.okx.com"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["port"], 443)

    def test_events_clamps_limit(self):
        out = self.routes["/api/doctor/events"]({"limit": 99999})
        self.assertTrue(out["ok"])

    def test_fix_requires_action(self):
        out = self.routes["/api/doctor/fix"]({})
        self.assertFalse(out["ok"])
        self.assertIn("action", out["error"])

    def test_fix_runs_action(self):
        out = self.routes["/api/doctor/fix"]({"action": "ACT-FLUSH-DNS", "why": "测试"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["run"]["act_id"], "ACT-FLUSH-DNS")
        self.assertEqual(out["run"]["by"], "user")

    def test_undo_requires_run_id(self):
        out = self.routes["/api/doctor/undo"]({})
        self.assertFalse(out["ok"])

    def test_state_has_doctor_light(self):
        st = webui._doctor_state(self.app)
        self.assertTrue(st["enabled"])
        self.assertFalse(st["started"])
        self.assertEqual(st["version"], "9.9.9")

    def test_state_doctor_disabled_by_flag(self):
        self.app.settings["doctor"] = False
        st = webui._doctor_state(self.app)
        self.assertFalse(st["enabled"])
        self.assertTrue(st["error"])

    def test_state_doctor_survives_broken_light(self):
        orig = doctor.light
        doctor.light = lambda app: (_ for _ in ()).throw(OSError("wmi 挂了"))
        try:
            st = webui._doctor_state(self.app)
        finally:
            doctor.light = orig
        self.assertFalse(st["available"])
        self.assertIn("OSError", st["error"])


if __name__ == "__main__":
    unittest.main()
