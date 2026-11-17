"""直连白名单（1.3.1 direct-sites）的离线测试：不联网、不碰真实 data 目录。

为什么单独测：这一层解决的是「订阅更新 / 升级之后，手工加在 config.yaml 里的
DIRECT 规则会丢」—— 规则丢了不会报错，只会让某个站点又变成走海外节点、
连接被重置，用户以为「软件又坏了」。所以三条必须钉死：写进的是模块文件、
重建配置真的被触发、重复添加不会白 reload 一次内核。
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vpntray import app as app_mod  # noqa: E402
from vpntray import modules, paths, webui  # noqa: E402


@contextlib.contextmanager
def sandbox():
    old = paths.MODULES
    d = pathlib.Path(tempfile.mkdtemp(prefix="vpndirect-"))
    paths.MODULES = d
    modules._cache["sig"] = None
    try:
        yield d
    finally:
        paths.MODULES = old
        modules._cache["sig"] = None
        shutil.rmtree(d, ignore_errors=True)


class DirectModuleTest(unittest.TestCase):
    def setUp(self):
        self._cm = sandbox()
        self.dir = self._cm.__enter__()

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def test_add_creates_module_file(self):
        ok, err = modules.direct_add("zcstzhihui.icu")
        self.assertTrue(ok, err)
        self.assertEqual(modules.direct_domains(), ["zcstzhihui.icu"])
        f = pathlib.Path(self.dir) / (modules.DIRECT_KEY + ".json")
        self.assertTrue(f.exists())

    def test_add_normalizes_wildcard_prefix(self):
        modules.direct_add("*.Example.COM.")
        self.assertEqual(modules.direct_domains(), ["example.com"])

    def test_add_rejects_nonsense(self):
        for bad in ("", "   ", "http://a.com/x", "a", "*", "a b.com"):
            ok, err = modules.direct_add(bad)
            self.assertFalse(ok, bad)
            self.assertTrue(err)
        self.assertEqual(modules.direct_domains(), [])

    def test_add_is_idempotent(self):
        modules.direct_add("a.example.com")
        ok, err = modules.direct_add("A.example.com")
        self.assertTrue(ok, err)
        self.assertEqual(modules.direct_domains(), ["a.example.com"])

    def test_remove_drops_file_when_empty(self):
        modules.direct_add("a.example.com")
        modules.direct_add("b.example.com")
        ok, err = modules.direct_remove("a.example.com")
        self.assertTrue(ok, err)
        self.assertEqual(modules.direct_domains(), ["b.example.com"])
        ok, err = modules.direct_remove("b.example.com")
        self.assertTrue(ok, err)
        self.assertEqual(modules.direct_domains(), [])
        f = pathlib.Path(self.dir) / (modules.DIRECT_KEY + ".json")
        self.assertFalse(f.exists())

    def test_remove_unknown_reports(self):
        ok, err = modules.direct_remove("nope.example.com")
        self.assertFalse(ok)
        self.assertIn("nope.example.com", err)

    def test_cap(self):
        old = modules.MAX_DIRECT
        modules.MAX_DIRECT = 2
        try:
            self.assertTrue(modules.direct_add("a.example.com")[0])
            self.assertTrue(modules.direct_add("b.example.com")[0])
            ok, err = modules.direct_add("c.example.com")
            self.assertFalse(ok)
            self.assertIn("最多", err)
        finally:
            modules.MAX_DIRECT = old

    def test_disabled_module_means_no_domains(self):
        modules.direct_add("a.example.com")
        ok, err = modules.update(modules.DIRECT_KEY, enabled=False)
        self.assertTrue(ok, err)
        self.assertEqual(modules.direct_domains(), [])

    def test_module_shape(self):
        modules.direct_add("a.example.com")
        mod = modules.load(modules.DIRECT_KEY)
        self.assertEqual(mod["kind"], "rule")
        self.assertEqual(mod["group"]["target"], "DIRECT")
        self.assertEqual(mod["order"], 1)
        self.assertTrue(mod["enabled"])
        self.assertEqual(mod["match"]["domains"], ["a.example.com"])


class DirectAppTest(unittest.TestCase):
    """app 层：真方法绑到假宿主上，只检查「重建配置有没有被触发」。"""

    def setUp(self):
        self._cm = sandbox()
        self._cm.__enter__()
        self.calls = []
        calls = self.calls

        class Host:
            def direct_list(inner):
                return modules.direct_domains()

            def _rebuild_from_modules(inner, notify=""):
                calls.append(notify)
                return True, ""

        self.host = Host()
        self.host.direct_add = types.MethodType(app_mod.App.direct_add, self.host)
        self.host.direct_remove = types.MethodType(app_mod.App.direct_remove, self.host)

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def test_add_rebuilds_config(self):
        ok, err = self.host.direct_add("zcstzhihui.icu")
        self.assertTrue(ok, err)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("zcstzhihui.icu", self.calls[0])

    def test_duplicate_add_does_not_rebuild(self):
        self.host.direct_add("a.example.com")
        self.calls.clear()
        ok, err = self.host.direct_add("a.example.com")
        self.assertTrue(ok, err)
        self.assertEqual(self.calls, [])

    def test_bad_domain_does_not_rebuild(self):
        ok, err = self.host.direct_add("not a domain")
        self.assertFalse(ok)
        self.assertEqual(self.calls, [])

    def test_remove_rebuilds_config(self):
        self.host.direct_add("a.example.com")
        self.calls.clear()
        ok, err = self.host.direct_remove("a.example.com")
        self.assertTrue(ok, err)
        self.assertEqual(len(self.calls), 1)

class DirectRouteTest(unittest.TestCase):
    """WebUI 的三条直连路由：不起 HTTP 服务，直接调 _routes_scene() 里的 handler。"""

    def setUp(self):
        self._cm = sandbox()
        self._cm.__enter__()
        self.calls = []
        calls = self.calls

        class Host(dict):
            def save(inner):
                pass

        a = Host({"data_dir": "E:" + os.sep + "tmp" + os.sep + "webui-direct",
                  "doctor": False, "doctor_actions": True})

        def direct_list(_b=None):
            return {"ok": True, "domains": modules.direct_domains()}

        def direct_add(body):
            d = str((body or {}).get("domain") or "").strip()
            if not d:
                return {"ok": False, "error": "要填域名", "domains": modules.direct_domains()}
            ok, err = modules.direct_add(d)
            if ok:
                calls.append(d)
            return {"ok": ok, "error": err, "domains": modules.direct_domains()}

        def direct_remove(body):
            d = str((body or {}).get("domain") or "").strip()
            ok, err = modules.direct_remove(d)
            return {"ok": ok, "error": err, "domains": modules.direct_domains()}

        a.direct_list = lambda: modules.direct_domains()
        def _add(d):
            ok, err = modules.direct_add(d)
            if ok:
                calls.append(d)
            return ok, err

        a.direct_add = _add
        a.direct_remove = lambda d: modules.direct_remove(d)
        self.ui = webui.WebUI(a, lambda: None, port=0)
        r = self.ui._routes_scene()
        self.assertTrue(callable(r.get("/api/direct")))
        self.assertTrue(callable(r.get("/api/direct/add")))
        self.assertTrue(callable(r.get("/api/direct/remove")))
        self.routes = r

    def tearDown(self):
        self._cm.__exit__(None, None, None)

    def test_list_empty(self):
        out = self.routes["/api/direct"](None)
        self.assertTrue(out["ok"])
        self.assertEqual(out["domains"], [])

    def test_add_then_list(self):
        out = self.routes["/api/direct/add"]({"domain": "zcstzhihui.icu"})
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["domains"], ["zcstzhihui.icu"])
        self.assertEqual(self.calls, ["zcstzhihui.icu"])

    def test_add_requires_domain(self):
        out = self.routes["/api/direct/add"]({})
        self.assertFalse(out["ok"])
        self.assertIn("要填域名", out["error"])

    def test_add_bad_domain_reports(self):
        out = self.routes["/api/direct/add"]({"domain": "http://a.com/x"})
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"])
        self.assertEqual(self.calls, [])

    def test_remove(self):
        self.routes["/api/direct/add"]({"domain": "a.example.com"})
        out = self.routes["/api/direct/remove"]({"domain": "a.example.com"})
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["domains"], [])

    def test_state_carries_domains(self):
        modules.direct_add("a.example.com")
        st = webui._direct_state(self.ui.app)
        self.assertEqual(st["domains"], ["a.example.com"])


if __name__ == "__main__":
    unittest.main()
