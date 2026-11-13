# -*- coding: utf-8 -*-
"""configgen 回归：非 select 类型的模块也必须插规则。

审查里实测过一次：模块组按 url-test / fallback 建出来了，但 scene_on 用
type == "select" 重算了一遍，于是规则一条都没插，流量全落到 MATCH 主组 ——
用户看到「组在、也选了」，实际加速完全没生效。这组用例锁死这个行为。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import configgen  # noqa: E402


class _S(dict):
    def save(self) -> None:
        self["_saved"] = True


def _sub():
    return {
        "proxies": [{"name": "HK-1", "type": "ss", "server": "1.1.1.1", "port": 443,
                     "cipher": "aes-128-gcm", "password": "x"},
                    {"name": "JP-1", "type": "ss", "server": "1.1.1.2", "port": 443,
                     "cipher": "aes-128-gcm", "password": "x"}],
        "proxy-groups": [{"name": "节点选择", "type": "select",
                          "proxies": ["HK-1", "JP-1"]}],
        "rules": ["MATCH,节点选择"],
    }


def _settings(**kw):
    s = _S(mixed_port=7891, ai_enabled=False, scene_auto=True,
           ai_block_regions=["香港"], ai_prefer_regions=["日本", "新加坡"])
    s.update(kw)
    return s


def _mod(key, title, gtype, kind="scene", target="", domains=None, pinned=""):
    return {
        "schema": 1, "key": key, "title": title, "desc": "", "kind": kind,
        "enabled": True, "builtin": False, "icon": "game", "order": 10,
        "group": {"type": gtype, "auto": False, "auto_title": "", "probe": "",
                  "interval": 0, "timeout": 0, "target": target},
        "match": {"domains": domains or ["epic.example"], "rules": [], "cidrs": []},
        "prefer_regions": [], "pinned": pinned,
    }


class SceneGroupTypes(unittest.TestCase):
    def setUp(self):
        self._old_scenes = configgen.scenes
        self._old_all = configgen.all_modules
        self.nodes = ["HK-1", "JP-1"]

    def tearDown(self):
        configgen.scenes = self._old_scenes
        configgen.all_modules = self._old_all

    def _install(self, mods):
        configgen.scenes = lambda: [m for m in mods if m["kind"] == "scene"]
        configgen.all_modules = lambda: list(mods)

    def _rules_for(self, mods, delays=None):
        self._install(mods)
        settings = _settings()
        groups, scene_on = configgen._scene_groups(self.nodes, settings, delays or {})
        rules = configgen._rules({"rules": ["GEOIP,US,PROXY"]}, "节点选择",
                                 False, scene_on, groups)
        return groups, scene_on, rules

    def test_url_test_module_gets_rules(self):
        groups, scene_on, rules = self._rules_for([_mod("epic", "Epic 加速", "url-test")])
        self.assertEqual(groups[0]["type"], "url-test")
        self.assertIn("tolerance", groups[0])
        self.assertIn("Epic 加速", scene_on)
        self.assertIn("DOMAIN-SUFFIX,epic.example,Epic 加速", rules)

    def test_fallback_module_gets_rules(self):
        groups, _, rules = self._rules_for([_mod("epic", "Epic 加速", "fallback")])
        self.assertEqual(groups[0]["type"], "fallback")
        self.assertIn("DOMAIN-SUFFIX,epic.example,Epic 加速", rules)

    def test_load_balance_module_gets_rules(self):
        _, _, rules = self._rules_for([_mod("epic", "Epic 加速", "load-balance")])
        self.assertIn("DOMAIN-SUFFIX,epic.example,Epic 加速", rules)

    def test_select_module_gets_rules(self):
        _, _, rules = self._rules_for([_mod("epic", "Epic 加速", "select")])
        self.assertIn("DOMAIN-SUFFIX,epic.example,Epic 加速", rules)

    def test_rule_module_targets_main(self):
        _, _, rules = self._rules_for([_mod("ad", "广告拦截", "select", kind="rule", target="")])
        self.assertIn("DOMAIN-SUFFIX,epic.example,节点选择", rules)

    def test_rule_module_unknown_target_falls_back(self):
        _, _, rules = self._rules_for(
            [_mod("ad", "广告拦截", "select", kind="rule", target="不存在的组")])
        self.assertIn("DOMAIN-SUFFIX,epic.example,节点选择", rules)

    def test_duplicate_group_name_skipped_in_build(self):
        # 两个模块同名：build 必须只留一个组，否则 mihomo 直接起不来
        mods = [_mod("a", "同名", "select", domains=["a.example"]),
                _mod("b", "同名", "url-test", domains=["b.example"])]
        self._install(mods)
        cfg = configgen.build(_sub(), _settings())
        names = [g["name"] for g in cfg["proxy-groups"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names.count("同名"), 1)
        # 规则只能有一条指向它（第二条规则会被同名组吃掉）
        hits = [r for r in cfg["rules"] if r.endswith(",同名")]
        self.assertEqual(len(hits), 2)   # a.example + b.example，都指向保留下来的那个组
        self.assertTrue(all(r.split(",")[1] in ("a.example", "b.example") for r in hits))

    def test_scene_rule_group_name_collides_with_subscription(self):
        # 模块组名和订阅组重名时，模块组跳过、规则也不能指向它
        mods = [_mod("epic", "节点选择", "select")]
        self._install(mods)
        cfg = configgen.build(_sub(), _settings())
        names = [g["name"] for g in cfg["proxy-groups"]]
        self.assertEqual(names.count("节点选择"), 1)
        self.assertFalse([r for r in cfg["rules"] if r.endswith(",节点选择")
                          and "epic.example" in r])

    def test_module_without_nodes_builds_nothing(self):
        self._install([_mod("epic", "Epic 加速", "url-test")])
        groups, scene_on = configgen._scene_groups([], _settings(), {})
        self.assertEqual(groups, [])
        self.assertEqual(scene_on, set())

    def test_main_group_fallback_saves_settings(self):
        # _main_group 回退后要落盘，否则每次启动都重新 warning
        settings = _settings(main_group="不存在的组")
        sub = {"proxy-groups": [{"name": "节点选择", "type": "select", "proxies": []}]}
        got = configgen._main_group(sub, settings)
        self.assertEqual(got, "节点选择")
        self.assertTrue(settings.get("_saved"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
