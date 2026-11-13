"""更新仓库填写的收敛与保存（3.0.1）。

重点不是「能查更新」，而是用户手填 owner/name 时那些常见写法都不能让功能看起来坏掉：
https://github.com/a/b 、a/b/ 、a/b.git 、前后空格 —— 全部收敛成 a/b。
"""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpntray import app as app_mod
from vpntray import updater


class NormRepo(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(updater.normalize_repo("ayuanjc/VPNTray")[:2], (True, "ayuanjc/VPNTray"))

    def test_full_url(self):
        ok, val, _ = updater.normalize_repo("https://github.com/ayuanjc/VPNTray")
        self.assertTrue(ok)
        self.assertEqual(val, "ayuanjc/VPNTray")

    def test_trailing_slash_and_dot_git(self):
        ok, val, _ = updater.normalize_repo("  ayuanjc/VPNTray.git/  ")
        self.assertTrue(ok)
        self.assertEqual(val, "ayuanjc/VPNTray")

    def test_extra_path_dropped(self):
        ok, val, _ = updater.normalize_repo("github.com/a/b/releases/tag/v1")
        self.assertTrue(ok)
        self.assertEqual(val, "a/b")

    def test_missing_name(self):
        ok, val, err = updater.normalize_repo("ayuanjc")
        self.assertFalse(ok)
        self.assertIn("owner/name", err)

    def test_empty(self):
        ok, _, err = updater.normalize_repo("   ")
        self.assertFalse(ok)
        self.assertIn("留空", err)

    def test_illegal_chars(self):
        ok, _, err = updater.normalize_repo("a b/c d")
        self.assertFalse(ok)


class _Settings(dict):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.saved = 0

    def save(self):
        self.saved += 1
        return True


def _fake_app(**settings):
    calls = []

    def _changed(self=None):
        calls.append("changed")

    def update_check(self=None, force=False):
        calls.append(("check", force))
        return {"ok": True, "latest": "9.9.9"}

    # 桩里不能直接用 MethodType(self=None)：绑定要求真实例。改用带默认 self 的
    # 普通函数，App.update_set_repo 里的 self._changed() / self.update_check(force=True)
    # 调用过去正好把 self 丢掉。
    fake = types.SimpleNamespace(
        settings=_Settings(**settings),
        state={"update": {"ok": True, "latest": "1.0.0"}},
        _lock=__import__("threading").RLock(),
        _changed=_changed,
        update_check=update_check,
    )
    fake.calls = calls
    return fake


class SetRepo(unittest.TestCase):
    def test_saves_and_rechecks(self):
        fake = _fake_app(update_repo="")
        out = app_mod.App.update_set_repo(fake, "https://github.com/ayuanjc/VPNTray/")
        self.assertTrue(out["ok"])
        self.assertEqual(out["repo"], "ayuanjc/VPNTray")
        self.assertEqual(fake.settings["update_repo"], "ayuanjc/VPNTray")
        self.assertEqual(fake.settings.saved, 1)
        self.assertIn(("check", True), fake.calls)
        self.assertTrue(any(c == "changed" for c in fake.calls))

    def test_invalid_keeps_old_value(self):
        fake = _fake_app(update_repo="a/b")
        out = app_mod.App.update_set_repo(fake, "不是仓库")
        self.assertFalse(out["ok"])
        self.assertEqual(fake.settings["update_repo"], "a/b")
        self.assertEqual(fake.settings.saved, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
