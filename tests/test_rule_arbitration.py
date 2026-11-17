# -*- coding: utf-8 -*-
"""规则仲裁（1.3.0）：同一个域只能属于一个组。

真机现场：gemini.google.com 在 9,421 条规则里出现 3–4 次（Google 模块 /
订阅的「AI 网站」/「媒体解锁」），mihomo 按顺序求值，后几条永远是死规则。
界面显示模块选中了日本节点，实际出口却是订阅组 —— 这种错乱靠肉眼几乎看不见，
所以把它锁进测试。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import configgen, modules  # noqa: E402


class _S(dict):
    def save(self) -> None:
        self["_saved"] = True


def _sub():
    return {
        "proxies": [
            {"name": "HK-1", "type": "ss", "server": "1.1.1.1", "port": 443,
             "cipher": "aes-128-gcm", "password": "x"},
            {"name": "JP-1", "type": "ss", "server": "1.1.1.2", "port": 443,
             "cipher": "aes-128-gcm", "password": "x"},
        ],
        "proxy-groups": [{"name": "节点选择", "type": "select", "proxies": ["HK-1", "JP-1"]}],
        "rules": ["MATCH,节点选择"],
    }


def _settings(**kw):
    s = _S(mixed_port=7891, ai_enabled=False, scene_auto=True,
           ai_block_regions=["香港"], ai_prefer_regions=["日本", "新加坡"])
    s.update(kw)
    return s


def _mod(key, title, domains=None, rules=None, exclude=None, auto=False, gtype="select"):
    return {
        "schema": 1, "key": key, "title": title, "desc": "", "kind": "scene",
        "enabled": True, "builtin": False, "icon": "x", "order": 10,
        "group": {"type": gtype, "auto": auto, "auto_title": title.split()[0] + " 自动",
                  "probe": "https://example.com/204", "interval": 180, "timeout": 3000,
                  "target": ""},
        "match": {"domains": domains or [], "rules": rules or [], "cidrs": [],
                  "exclude": exclude or []},
        "prefer_regions": [], "pinned": "",
    }


def _suffix(cfg, d):
    head = "domain-suffix," + d.lower() + ","
    return [r for r in cfg["rules"] if r.lower().startswith(head)]


class _Base(unittest.TestCase):
    def setUp(self):
        self._old_scenes = configgen.scenes
        self._old_all = configgen.all_modules

    def tearDown(self):
        configgen.scenes = self._old_scenes
        configgen.all_modules = self._old_all

    def _cfg(self, mods, **kw):
        configgen.scenes = lambda: [m for m in mods if m.get("kind") == "scene"]
        configgen.all_modules = lambda: list(mods)
        return configgen.build(_sub(), _settings(**kw))


class DupTest(_Base):
    def test_same_domain_kept_once(self):
        cfg = self._cfg([_mod("a", "A 加速", ["same.example"]),
                         _mod("b", "B 加速", ["same.example"])])
        self.assertEqual(len(_suffix(cfg, "same.example")), 1)
        self.assertIn("A 加速", _suffix(cfg, "same.example")[0])

    def test_no_duplicate_suffix_anywhere(self):
        cfg = self._cfg([_mod("a", "A 加速", ["x.example", "shared.example"]),
                         _mod("b", "B 加速", ["shared.example", "y.example"])])
        seen = {}
        for r in cfg["rules"]:
            low = r.lower()
            if not low.startswith("domain-suffix,"):
                continue
            dom = low.split(",")[1]
            seen[dom] = seen.get(dom, 0) + 1
        dup = {k: v for k, v in seen.items() if v > 1}
        self.assertEqual(dup, {})


class AiWinTest(_Base):
    """AI 会话域必须先于模块域被占：Gemini 的 IP 不能跟着模块的自动切换漂。"""

    def test_ai_takes_gemini_over_module(self):
        cfg = self._cfg([_mod("g", "Google 加速", ["gemini.google.com", "google.com"])],
                        ai_enabled=True)
        rows = _suffix(cfg, "gemini.google.com")
        self.assertEqual(len(rows), 1)
        self.assertIn(configgen.AI_GROUP, rows[0])
        # 同一模块里没和 AI 撞的域照样归模块
        self.assertIn("Google 加速", _suffix(cfg, "google.com")[0])


class ExcludeTest(_Base):
    def test_exclude_emits_direct_before_module_rule(self):
        cfg = self._cfg([_mod("g", "Google 加速", ["google.com"], exclude=["dl.google.com"])])
        dl = [i for i, r in enumerate(cfg["rules"])
              if r.lower() == "domain-suffix,dl.google.com,direct"]
        g = [i for i, r in enumerate(cfg["rules"])
             if r.lower() == "domain-suffix,google.com,google 加速"]
        self.assertEqual(len(dl), 1)
        self.assertEqual(len(g), 1)
        self.assertLess(dl[0], g[0], "排除行必须排在模块规则之前才生效")

    def test_exclude_does_not_swallow_other_module(self):
        cfg = self._cfg([_mod("g", "Google 加速", ["google.com"], exclude=["dl.google.com"]),
                         _mod("d", "下载加速", ["dl.google.com"])])
        rows = _suffix(cfg, "dl.google.com")
        self.assertEqual(len(rows), 1)
        self.assertIn("DIRECT", rows[0])


class AutoGroupTest(_Base):
    def test_auto_subgroup_is_url_test(self):
        cfg = self._cfg([_mod("g", "Google 加速", ["google.com"], auto=True)])
        subs = [g for g in cfg["proxy-groups"] if g["name"] == "Google 自动"]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["type"], "url-test")
        self.assertEqual(subs[0]["tolerance"], 50)
        self.assertTrue(subs[0]["url"])


class BuiltinGoogleTest(unittest.TestCase):
    def setUp(self):
        self.google = {m["key"]: m for m in modules._launcher_builtins()}["google"]

    def test_no_wide_googleapis(self):
        self.assertNotIn("googleapis.com", self.google["match"]["domains"])
        self.assertEqual(self.google["match"]["rules"], [])

    def test_exclude_has_direct_subs(self):
        ex = self.google["match"]["exclude"]
        for d in ("dl.google.com", "mtalk.google.com"):
            self.assertIn(d, ex)

    def test_desc_and_probe(self):
        self.assertIn("Antigravity", self.google["desc"])
        self.assertTrue(self.google["group"]["probe"].startswith("https://gemini.google.com"))


if __name__ == "__main__":
    unittest.main()