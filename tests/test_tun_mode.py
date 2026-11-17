# -*- coding: utf-8 -*-
"""TUN 模式回归（1.2.7）。

真机撞出来的两个 bug：
  1) configgen 只给 tun.enable 不给 auto-route，内核会建 wintun 网卡但不导流，
     用户看到「TUN 已开」而实际流量还走系统代理 / 直连。
  2) set_tun 只改 settings 不重写 config.yaml，内核重启后读到的还是 enable: false，
     表现为接口返回 ok、state.tun=true，而 /configs 里 tun.enable 一直是 false。
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import app as app_mod, configgen  # noqa: E402


class _S(dict):
    def save(self) -> None:
        self["_saved"] = True


def _sub():
    return {
        "proxies": [{"name": "HK-1", "type": "ss", "server": "1.1.1.1", "port": 443,
                     "cipher": "aes-128-gcm", "password": "x"}],
        "proxy-groups": [{"name": "节点选择", "type": "select", "proxies": ["HK-1"]}],
        "rules": ["MATCH,节点选择"],
    }


def _settings(**kw):
    s = _S(mixed_port=7891, ai_enabled=False, scene_auto=False,
           ai_block_regions=[], ai_prefer_regions=[])
    s.update(kw)
    return s


class ConfigTunTest(unittest.TestCase):
    def test_tun_on_sets_enable_and_auto_route(self):
        cfg = configgen.build(_sub(), _settings(tun=True), {})
        self.assertTrue(cfg["tun"]["enable"])
        self.assertTrue(cfg["tun"]["auto-route"], "auto-route 没跟着开：网卡在但不导流")

    def test_tun_off_both_false(self):
        cfg = configgen.build(_sub(), _settings(tun=False), {})
        self.assertFalse(cfg["tun"]["enable"])
        self.assertFalse(cfg["tun"]["auto-route"])


class SetTunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ndtun-")
        open(os.path.join(self.tmp, "wintun.dll"), "wb").write(b"MZ")
        self._patches = [
            mock.patch.object(app_mod, "BIN", __import__("pathlib").Path(self.tmp)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _self(self, restart=True):
        st = _settings(tun=False, system_proxy=False)
        calls = {"write": 0, "restart": 0, "cfg": None}

        def _write(cfg):
            calls["write"] += 1
            calls["cfg"] = cfg
            return "config.yaml"

        def _restart():
            calls["restart"] += 1
            return restart

        fake = types.SimpleNamespace(
            settings=st,
            _work_try=lambda: True,
            _work_done=lambda: None,
            _set=lambda **kw: None,
            _delay_map=lambda: {},
            restart_kernel=_restart,
        )
        return fake, calls, _write

    def test_rewrites_config_before_restart(self):
        fake, calls, _write = self._self()
        with mock.patch.object(app_mod.netguard, "is_admin", return_value=True), \
                mock.patch.object(app_mod.subscription, "load_cache", return_value={"x": 1}), \
                mock.patch.object(app_mod.subscription, "normalize", return_value=_sub()), \
                mock.patch.object(app_mod.configgen, "write", side_effect=_write):
            out = app_mod.App.set_tun(fake, True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(calls["write"], 1, "没重写 config.yaml，内核读到的还是旧的")
        self.assertTrue(calls["cfg"]["tun"]["enable"])
        self.assertEqual(calls["restart"], 1)
        self.assertTrue(fake.settings["tun"])

    def test_no_cache_returns_error_and_keeps_old(self):
        fake, calls, _write = self._self()
        with mock.patch.object(app_mod.netguard, "is_admin", return_value=True), \
                mock.patch.object(app_mod.subscription, "load_cache", return_value=None):
            out = app_mod.App.set_tun(fake, True)
        self.assertFalse(out["ok"])
        self.assertIn("订阅", out["error"])
        self.assertEqual(calls["restart"], 0)
        self.assertFalse(fake.settings["tun"])

    def test_missing_wintun_refuses(self):
        os.remove(os.path.join(self.tmp, "wintun.dll"))
        fake, calls, _write = self._self()
        with mock.patch.object(app_mod.netguard, "is_admin", return_value=True), \
                mock.patch.object(app_mod.subscription, "load_cache", return_value={"x": 1}), \
                mock.patch.object(app_mod.subscription, "normalize", return_value=_sub()), \
                mock.patch.object(app_mod.configgen, "write", side_effect=_write):
            out = app_mod.App.set_tun(fake, True)
        self.assertFalse(out["ok"])
        self.assertIn("wintun", out["error"])
        self.assertEqual(calls["restart"], 0)

    def test_not_admin_refuses(self):
        fake, calls, _write = self._self()
        with mock.patch.object(app_mod.netguard, "is_admin", return_value=False):
            out = app_mod.App.set_tun(fake, True)
        self.assertFalse(out["ok"])
        self.assertIn("管理员", out["error"])
        self.assertEqual(calls["restart"], 0)


class DropTunWithoutAdminTest(unittest.TestCase):
    """普通权限启动时必须自己关掉 TUN，否则内核起不来、整机断网。"""

    def _fake(self, tun=True):
        st = _settings(tun=tun)
        state = {}
        fake = types.SimpleNamespace(settings=st, _set=lambda **kw: state.update(kw))
        return fake, state

    def test_non_admin_drops_tun(self):
        fake, state = self._fake()
        with mock.patch.object(app_mod.netguard, "is_admin", return_value=False):
            self.assertTrue(app_mod.App._drop_tun_without_admin(fake))
        self.assertFalse(fake.settings["tun"])
        self.assertFalse(state.get("tun"))
        self.assertIn("管理员", state.get("tun_reason") or "")

    def test_admin_keeps_tun(self):
        fake, state = self._fake()
        with mock.patch.object(app_mod.netguard, "is_admin", return_value=True):
            self.assertFalse(app_mod.App._drop_tun_without_admin(fake))
        self.assertTrue(fake.settings["tun"])
        self.assertEqual(state, {})

    def test_tun_off_is_noop(self):
        fake, state = self._fake(tun=False)
        with mock.patch.object(app_mod.netguard, "is_admin", return_value=False):
            self.assertFalse(app_mod.App._drop_tun_without_admin(fake))
        self.assertEqual(state, {})

if __name__ == "__main__":
    unittest.main()
