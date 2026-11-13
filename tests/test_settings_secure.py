"""settings 的凭据封装 / 坏文件抢救 / 结构迁移。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpntray import settings as S  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._old = (S.SETTINGS, S.MODULES)
        S.SETTINGS = self.dir / "settings.json"
        S.MODULES = self.dir / "modules"
        S.MODULES.mkdir()

    def tearDown(self):
        S.SETTINGS, S.MODULES = self._old
        shutil.rmtree(self.dir, ignore_errors=True)


class Seal(_Tmp):
    @unittest.skipUnless(os.name == "nt", "DPAPI 只在 Windows 上")
    def test_roundtrip(self):
        sealed = S._seal("hello-token-1234567890")
        self.assertTrue(sealed.startswith(S._DPAPI_PREFIX))
        self.assertNotIn("hello-token", sealed)
        self.assertEqual(S._unseal(sealed), "hello-token-1234567890")

    def test_plain_passthrough(self):
        self.assertEqual(S._seal(""), "")
        self.assertEqual(S._unseal("plain"), "plain")

    def test_broken_blob_returns_none(self):
        self.assertIsNone(S._unseal(S._DPAPI_PREFIX + "!!!notbase64"))

    @unittest.skipUnless(os.name == "nt", "DPAPI 只在 Windows 上")
    def test_save_seals_and_load_unseals(self):
        token = "t" * 32
        s = S.Settings()
        s["ai_api_token"] = token
        s.save()
        raw = json.loads(S.SETTINGS.read_text(encoding="utf-8"))
        self.assertTrue(raw["ai_api_token"].startswith(S._DPAPI_PREFIX))
        self.assertTrue(raw["secret"].startswith(S._DPAPI_PREFIX))
        again = S.Settings()
        self.assertEqual(again.get("ai_api_token"), token)
        self.assertEqual(len(again.get("secret")), 32)


class Salvage(_Tmp):
    def test_broken_json_keeps_good_keys(self):
        broken = (
            '{\n'
            '  "mixed_port": 7891,\n'
            '  "sub_url": "https://example.com/sub",\n'
            '  "subs": [{"name": "a", "url": "https://example.com/sub"}],\n'
            '  "oops": }\n'
            '}'
        )
        S.SETTINGS.write_text(broken, encoding="utf-8")
        s = S.Settings()
        self.assertEqual(s.get("mixed_port"), 7891)
        self.assertEqual(s.get("sub_url"), "https://example.com/sub")
        self.assertEqual(len(s.get("subs") or []), 1)
        # 坏文件必须留档，用户才有救回来的凭据
        self.assertTrue(list(self.dir.glob("settings.bad-*.json")))

    def test_type_mismatch_backed_up(self):
        S.SETTINGS.write_text('{"mixed_port": "not-a-port"}', encoding="utf-8")
        s = S.Settings()
        self.assertEqual(s.get("mixed_port"), 7891)
        self.assertTrue(list(self.dir.glob("settings.json.bad-*")))


class Migrate(_Tmp):
    def test_drops_legacy_keys_after_seeding(self):
        (S.MODULES / "steam.json").write_text("{}", encoding="utf-8")
        S.SETTINGS.write_text(json.dumps({
            "steam_enabled": False, "steam_node": "HK", "steam_prefer_regions": ["HK"],
            "github_enabled": True, "github_node": "JP", "github_prefer_regions": [],
            "ai_node": "keep-me",
        }), encoding="utf-8")
        s = S.Settings()
        s.migrate()
        raw = json.loads(S.SETTINGS.read_text(encoding="utf-8"))
        for k in S.LEGACY_KEYS:
            self.assertNotIn(k, raw)
        self.assertEqual(raw["ai_node"], "keep-me")      # 不是场景键，必须留着
        self.assertEqual(raw["schema_version"], S.SCHEMA_VERSION)

    def test_keeps_keys_when_modules_missing(self):
        S.SETTINGS.write_text(json.dumps({"steam_node": "HK"}), encoding="utf-8")
        s = S.Settings()
        s.migrate()
        self.assertEqual(s.get("steam_node"), "HK")
        self.assertEqual(s.get("schema_version"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
