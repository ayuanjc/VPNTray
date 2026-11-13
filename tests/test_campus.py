"""校园网自动登录的离线用例。

不打真网络：_session() 被替换成假会话，204 复验也走桩。
覆盖的是「门户长什么样 → 怎么填」和「掉线了怎么办」两条主线。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import campus  # noqa: E402


class FakeResp:
    def __init__(self, code=200, text="", headers=None, url="http://172.32.253.17/"):
        self.status_code = code
        self.text = text
        self.headers = headers or {}
        self.url = url


class FakeSess:
    """够用的假 Session：get/post 按顺序吐预设响应，并记下调用参数。"""

    def __init__(self, get=None, post=None):
        self.headers = {}
        self.proxies = {}
        self.trust_env = True
        self._get = list(get or [])
        self._post = list(post or [])
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self._get.pop(0) if self._get else FakeResp(404)

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self._post.pop(0) if self._post else FakeResp(404)


LOGIN_HTML = """<html><body>
<form action="/login.do" method="post">
  <input type="hidden" name="wlan_ac_ip" value="10.0.0.1">
  <input type="text" name="username">
  <input type="password" name="password">
  <input type="submit" value="登录">
</form></body></html>"""

CAPTCHA_HTML = """<html><body>
<form action="/self/selfLogin.do" method="post">
  <input type="text" name="account">
  <input type="password" name="pwd">
  <input type="text" name="authCode">
</form></body></html>"""


class Detect(unittest.TestCase):
    def test_kind_by_url(self):
        self.assertEqual(campus.detect_kind("http://1.2.3.4/drcom/login?x=1"), "drcom")
        self.assertEqual(campus.detect_kind("http://1.2.3.4/cgi-bin/srun_portal"), "srun")
        self.assertEqual(campus.detect_kind("http://1.2.3.4/eportal/portal/login"), "eportal")
        self.assertEqual(campus.detect_kind("http://1.2.3.4/self/selfLogin.do"), "portal")

    def test_kind_by_body(self):
        self.assertEqual(campus.detect_kind("http://1.2.3.4/x", "srun 深澜认证"), "srun")
        self.assertEqual(campus.detect_kind("http://1.2.3.4/x", "Dr.COM 城市热点"), "drcom")
        self.assertEqual(campus.detect_kind("", ""), "portal")


class ParseForm(unittest.TestCase):
    def test_pick_password_form(self):
        f = campus.parse_form(LOGIN_HTML, "http://172.32.253.17/")
        self.assertIsNotNone(f)
        self.assertEqual(f["action"], "http://172.32.253.17/login.do")
        self.assertEqual(f["user_field"], "username")
        self.assertEqual(f["pass_field"], "password")
        self.assertFalse(f["captcha"])
        names = {x["name"]: x["value"] for x in f["fields"]}
        self.assertEqual(names["wlan_ac_ip"], "10.0.0.1", "hidden 字段的默认值要带上")

    def test_captcha_detected(self):
        f = campus.parse_form(CAPTCHA_HTML, "http://172.32.253.17/")
        self.assertTrue(f["captcha"], "authCode 字段要认成验证码")

    def test_no_form(self):
        self.assertIsNone(campus.parse_form("<html>没有表单</html>", "http://x/"))


class Discover(unittest.TestCase):
    def test_online_204(self):
        sess = FakeSess(get=[FakeResp(204, url=campus.PROBE_204)])
        with mock.patch.object(campus, "_session", lambda: sess):
            info = campus.discover()
        self.assertTrue(info["online"])
        self.assertEqual(info["portal"], "")

    def test_captive_redirect(self):
        sess = FakeSess(get=[FakeResp(302, headers={
            "Location": "http://172.32.253.17/drcom/login?callback=dr1003"})])
        with mock.patch.object(campus, "_session", lambda: sess):
            info = campus.discover()
        self.assertFalse(info["online"])
        self.assertIn("drcom", info["kind"])
        self.assertTrue(info["portal"].startswith("http://172.32.253.17/"))

    def test_offline(self):
        sess = FakeSess(get=[])
        sess.get = lambda *a, **k: (_ for _ in ()).throw(OSError("unreachable"))
        with mock.patch.object(campus, "_session", lambda: sess):
            info = campus.discover()
        self.assertFalse(info["online"])
        self.assertEqual(info["portal"], "")
        self.assertIn("失败", info["detail"])


class Login(unittest.TestCase):
    def test_requires_credentials(self):
        r = campus.login("auto", "http://1.2.3.4/", "", "")
        self.assertFalse(r["ok"])
        self.assertIn("账号", r["msg"])

    def test_custom_requires_template(self):
        r = campus.login("custom", "http://1.2.3.4/", "u", "p", template="")
        self.assertFalse(r["ok"])
        self.assertIn("模板", r["msg"])

    def test_unknown_provider_falls_back_to_auto(self):
        r = campus.login("bogus", "", "u", "p")
        self.assertFalse(r["ok"])
        self.assertIn(r["provider"], campus.PROVIDERS)

    def test_drcom_success(self):
        body = 'dr1003({"result":1,"msg":"认证成功"});'
        sess = FakeSess(get=[FakeResp(200, body)])
        with mock.patch.object(campus, "_session", lambda: sess), \
                mock.patch.object(campus, "verify", lambda *a, **k: True):
            r = campus.login("drcom", "http://172.32.253.17/", "20230001", "pw")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["provider"], "drcom")
        url = sess.calls[0][1]
        self.assertTrue(url.endswith("/drcom/login"))
        params = sess.calls[0][2]["params"]
        self.assertEqual(params["user_account"], ",0,20230001")

    def test_drcom_failure_message(self):
        sess = FakeSess(get=[FakeResp(200, 'dr1003({"result":0,"msg":"密码错误"})')])
        with mock.patch.object(campus, "_session", lambda: sess):
            r = campus.login("drcom", "http://172.32.253.17/", "u", "bad")
        self.assertFalse(r["ok"])
        self.assertIn("密码错误", r["msg"])

    def test_portal_captcha_needs_manual(self):
        sess = FakeSess(get=[FakeResp(200, CAPTCHA_HTML)])
        with mock.patch.object(campus, "_session", lambda: sess):
            r = campus.login("portal", "http://172.32.253.17/", "u", "p")
        self.assertFalse(r["ok"])
        self.assertTrue(r["need_manual"])

    def test_portal_form_submit(self):
        sess = FakeSess(get=[FakeResp(200, LOGIN_HTML)],
                        post=[FakeResp(302, "")])
        with mock.patch.object(campus, "_session", lambda: sess), \
                mock.patch.object(campus, "verify", lambda *a, **k: True):
            r = campus.login("portal", "http://172.32.253.17/", "20230001", "pw")
        self.assertTrue(r["ok"], r)
        method, url, kw = sess.calls[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(kw["data"]["username"], "20230001")
        self.assertEqual(kw["data"]["password"], "pw")
        self.assertEqual(kw["data"]["wlan_ac_ip"], "10.0.0.1")

    def test_custom_template_substitution(self):
        sess = FakeSess(get=[FakeResp(200, '{"result":1}')])
        with mock.patch.object(campus, "_session", lambda: sess), \
                mock.patch.object(campus, "verify", lambda *a, **k: True):
            r = campus.login("custom", "", "u1", "p1",
                             template="http://10.0.0.9/login?u={user}&p={pass}&ip={ip}")
        self.assertTrue(r["ok"], r)
        called = sess.calls[0][1]
        self.assertIn("u=u1", called)
        self.assertIn("p=p1", called)

    def test_mask_hides_password(self):
        self.assertNotIn("secret", campus._mask("user_password=secret&x=1"))


class GuardTick(unittest.TestCase):
    class App:
        def __init__(self, **kw):
            self.settings = {"campus_enabled": True, "campus_user": "20230001",
                             "campus_pass": "pw", "campus_provider": "auto",
                             "campus_template": "", "campus_interval": 30}
            self.settings.update(kw)
            self.recovered = []

        def on_campus_online(self, gw=""):
            self.recovered.append(gw)

    def test_online_no_action(self):
        g = campus.Guard(self.App())
        with mock.patch.object(campus, "discover",
                               lambda *a, **k: {"online": True, "portal": "", "kind": "",
                                                "detail": "204"}):
            out = g.tick()
        self.assertEqual(out["action"], "none")

    def test_offline_no_portal_does_not_login(self):
        g = campus.Guard(self.App())
        with mock.patch.object(campus, "discover",
                               lambda *a, **k: {"online": False, "portal": "", "kind": "",
                                                "detail": "超时"}):
            out = g.tick()
        self.assertEqual(out["action"], "offline")

    def test_need_config(self):
        app = self.App(campus_user="", campus_pass="")
        g = campus.Guard(app)
        with mock.patch.object(campus, "discover",
                               lambda *a, **k: {"online": False,
                                                "portal": "http://1.2.3.4/",
                                                "kind": "drcom", "detail": "302"}):
            out = g.tick()
        self.assertEqual(out["action"], "need_config")

    def test_login_ok_triggers_recover(self):
        app = self.App()
        g = campus.Guard(app)
        with mock.patch.object(campus, "discover",
                               lambda *a, **k: {"online": False,
                                                "portal": "http://1.2.3.4/",
                                                "kind": "drcom", "detail": "302"}), \
                mock.patch.object(campus, "login",
                                  lambda *a, **k: {"ok": True, "provider": "drcom"}), \
                mock.patch.object(campus, "gateway", lambda: "172.50.86.1"):
            out = g.tick()
        self.assertEqual(out["action"], "login_ok")
        self.assertEqual(app.recovered, ["172.50.86.1"])

    def test_failure_backoff(self):
        g = campus.Guard(self.App())
        with mock.patch.object(campus, "discover",
                               lambda *a, **k: {"online": False,
                                                "portal": "http://1.2.3.4/",
                                                "kind": "drcom", "detail": "302"}), \
                mock.patch.object(campus, "login", lambda *a, **k: {"ok": False, "msg": "x"}):
            g.tick()
            first = g._next_gap()
            g.tick()
            second = g._next_gap()
        self.assertEqual(first, 30)
        self.assertEqual(second, 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
