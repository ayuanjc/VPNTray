"""更新仓库填写的收敛与保存（3.0.1）。

重点不是「能查更新」，而是用户手填 owner/name 时那些常见写法都不能让功能看起来坏掉：
https://github.com/a/b 、a/b/ 、a/b.git 、前后空格 —— 全部收敛成 a/b。
"""
import base64
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vpntray import app as app_mod
from vpntray import (
    autostart, disguise, feedback, geolink, github_ops, purity, subscription, updater,
)
from vpntray import logger as vlog


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


# ══════════════════ 本轮审查条目回归（更新器 / 订阅 / 上游小模块） ══════════════════


class VersionCompare(unittest.TestCase):
    """P1-g：版本比较必须是逐段数字比较，不能只比主版本前缀。"""

    def test_patch_segment(self):
        # 3.0.10 > 3.0.9；按主版本前缀比会判成「一样」，永远不提示更新
        self.assertTrue(updater.is_newer("3.0.10", "3.0.9"))
        self.assertFalse(updater.is_newer("3.0.9", "3.0.10"))

    def test_equal_and_prefix(self):
        self.assertFalse(updater.is_newer("3.0.2", "3.0.2"))
        self.assertFalse(updater.is_newer("v3.0.2", "3.0.2"))
        self.assertTrue(updater.is_newer("3.1", "3.0.9"))

    def test_unparsable_is_not_newer(self):
        self.assertFalse(updater.is_newer("", "3.0.2"))
        self.assertIsNone(updater.parse_version(""))


class ZipSha(unittest.TestCase):
    """P1-f：zip 哈希的提取口径 —— 只认文件名匹配，别把 exe 的哈希当成包的。"""

    EXE = "a" * 64
    MIHOMO = "b" * 64
    ZIP = "c" * 64

    def test_match_by_basename(self):
        text = "\n".join([f"{self.EXE}  VPNTray.exe",
                          f"{self.MIHOMO}  bin/mihomo.exe",
                          f"{self.ZIP}  VPNTray-3.0.2-win64.zip"])
        self.assertEqual(updater._sha_from_text(text, "VPNTray-3.0.2-win64.zip"),
                         self.ZIP)

    def test_exe_hash_is_not_zip_hash(self):
        # 真实 release.ps1 的 SHA256SUMS.txt 只有 exe 和 mihomo 两行
        text = f"{self.EXE}  VPNTray.exe\n{self.MIHOMO}  bin/mihomo.exe\n"
        self.assertEqual(updater._sha_from_text(text, "VPNTray-3.0.2-win64.zip"), "")

    def test_single_hash_file(self):
        self.assertEqual(updater._sha_from_text(self.ZIP + "\n", "x.zip"), self.ZIP)


class PrepareNeedsSha(unittest.TestCase):
    """P1-f：拿不到 SHA256 时自动更新必须拒绝；手工确认后才继续。"""

    INFO = {
        "repo": "ayuanjc/VPNTray",
        "tag": "v3.0.2",
        "latest": "3.0.2",
        "zip_url": "https://github.com/ayuanjc/VPNTray/releases/download/v3.0.2/VPNTray-3.0.2-win64.zip",
        "zip_sha256": "",
    }

    def _patched(self):
        work = Path(tempfile.mkdtemp())
        return [
            mock.patch.object(updater, "_resolve_zip_sha", return_value=""),
            mock.patch.object(updater, "update_workdir", return_value=work),
            mock.patch.object(updater.os, "access", return_value=True),
        ]

    def test_auto_update_refused_without_sha(self):
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            with mock.patch.object(updater, "download",
                                   side_effect=AssertionError("缺哈希时不该下载")):
                with self.assertRaises(updater.UpdateError) as cm:
                    updater.prepare(dict(self.INFO))
        finally:
            for p in patches:
                p.stop()
        self.assertIn("无法校验", str(cm.exception))
        self.assertIn("已跳过自动更新", str(cm.exception))

    def test_confirm_true_proceeds(self):
        patches = self._patched()
        for p in patches:
            p.start()
        try:
            # 走到下载就说明确认后的分支真的放行了
            with mock.patch.object(updater, "download",
                                   side_effect=RuntimeError("DOWNLOAD_CALLED")):
                with self.assertRaises(RuntimeError):
                    updater.prepare(dict(self.INFO), confirm=True)
        finally:
            for p in patches:
                p.stop()


class SubDecode(unittest.TestCase):
    """P2：订阅编码兼容 —— BOM / GBK / URL-safe 无 padding 的 base64。"""

    def test_gbk_not_garbled(self):
        raw = "proxies:\n  - name: 香港01\n".encode("gbk")
        self.assertIn("香港01", subscription._decode_sub(raw))

    def test_bom_stripped(self):
        raw = "\ufeffproxies: []\n".encode("utf-8")
        self.assertTrue(subscription._decode_sub(raw).startswith("proxies"))

    def test_base64_urlsafe_unpadded(self):
        body = "proxies:\n  - name: A1\n    type: ss\n"
        b64 = base64.urlsafe_b64encode(body.encode()).decode()
        self.assertIn("proxies:", subscription._maybe_base64(b64))
        self.assertIn("proxies:", subscription._maybe_base64(b64.rstrip("=")))


def _cfg(extra_group=None):
    cfg = {
        "proxies": [{"name": "A", "type": "ss", "server": "1.1.1.1",
                     "port": 443, "cipher": "aes-128-gcm", "password": "x"}],
        "proxy-groups": [{"name": "主组", "type": "select", "proxies": ["A"]}],
        "rules": ["DOMAIN-SUFFIX,github.com,已删除的组"],
    }
    if extra_group:
        cfg["proxy-groups"].append(extra_group)
    return cfg


class SubParseEmptyProxies(unittest.TestCase):
    def test_yaml_with_empty_proxies(self):
        cfg = subscription.parse(
            b"proxies:\nproxy-groups:\n  - name: G\n    type: select\n"
            b"    proxies: [DIRECT]\nrules: []\n")
        self.assertEqual(cfg["proxies"], [])


class SubNormalizeRepair(unittest.TestCase):
    """P2：悬空策略组名改指主组；成员全空的组必须剔掉。"""

    def test_dangling_policy_points_to_main_group(self):
        out = subscription.normalize(_cfg())
        self.assertEqual(out["rules"], ["DOMAIN-SUFFIX,github.com,主组"])

    def test_empty_group_dropped(self):
        out = subscription.normalize(_cfg(
            {"name": "空组", "type": "select", "proxies": ["已删除节点"]}))
        self.assertEqual([g["name"] for g in out["proxy-groups"]], ["主组"])


class SaveCacheAtomic(unittest.TestCase):
    """P2：缓存写失败必须让调用方知道，不能照样返回哈希。"""

    def test_replace_failure_raises(self):
        with mock.patch("vpntray.subscription.os.replace",
                        side_effect=OSError("磁盘已满")):
            with self.assertRaises(subscription.SubscriptionError) as cm:
                subscription.save_cache(b"proxies: []\n", {"proxies": []})
        self.assertIn("写入订阅缓存失败", str(cm.exception))


class PurityOneIpRoute(unittest.TestCase):
    """P2：测速探测优先走本地代理，直连要在结果里标明。"""

    def test_direct_fallback_reported(self):
        seen = []

        def fake(url, proxies, timeout):
            seen.append(proxies)
            if proxies:
                return None            # 代理这一路失败
            return {"ip": "1.2.3.4"}  # 直连成功

        with mock.patch.object(purity, "_oneip_get", side_effect=fake):
            d = purity._oneip("1.2.3.4", port=7891)
        self.assertEqual(d["_via"], "direct")
        self.assertIsNotNone(seen[0])      # 第一条必须带本地代理
        self.assertIsNone(seen[1])

    def test_proxy_route_preferred(self):
        seen = []

        def fake(url, proxies, timeout):
            seen.append(proxies)
            return {"ip": "1.2.3.4"}

        with mock.patch.object(purity, "_oneip_get", side_effect=fake):
            d = purity._oneip("1.2.3.4", port=7891)
        self.assertEqual(d["_via"], "proxy")
        self.assertEqual(len(seen), 1)     # 代理通了就不再直连泄露本机 IP

    def test_host_is_a_constant(self):
        # 不再受 VPNTRAY_ONEIP_HOST 影响
        self.assertEqual(purity.ONEIP_HOST, "https://ip.net.coffee")


class PurityPutBatched(unittest.TestCase):
    """P2：put() 不再逐条落盘，攒一次原子写。"""

    def test_put_defers_and_flush_once(self):
        saved = []
        old_cache, old_mem = purity.CACHE, purity._mem
        purity.CACHE = Path(tempfile.mkdtemp()) / "purity.json"
        purity._mem = {}
        try:
            with mock.patch.object(purity, "_save", side_effect=lambda d: saved.append(d)):
                purity.put("n1", {"ip": "1.1.1.1", "ts": time.time()})
                purity.put("n2", {"ip": "2.2.2.2", "ts": time.time()})
                self.assertEqual(saved, [])          # 两次 put 都不落盘
                purity.flush()
            self.assertEqual(len(saved), 1)          # 只写一次，且两条都在
            self.assertEqual(set(saved[0]), {"n1", "n2"})
        finally:
            purity.CACHE, purity._mem = old_cache, old_mem


class FeedbackRedact(unittest.TestCase):
    """P2：反馈日志脱敏 + 只读尾部。"""

    def test_proxy_link_masked(self):
        s = feedback.redact("节点 ss://YWVzOnBhc3NAMS4yLjMuNDo4Mzg4#香港")
        self.assertIn("ss://***", s)
        self.assertNotIn("YWVz", s)

    def test_url_query_masked(self):
        s = feedback.redact("GET https://api.example.com/v1/x?token=abcdef&y=1 失败")
        self.assertNotIn("abcdef", s)

    def test_private_ip_masked(self):
        s = feedback.redact("监听 127.0.0.1 / 192.168.1.9 / 10.0.0.5")
        self.assertIn("内网IP", s)


class FeedbackTailLog(unittest.TestCase):
    def test_reads_only_tail(self):
        p = Path(tempfile.mkdtemp()) / "app.log"
        p.write_text("x" * (300 * 1024) + "\n最后一行\n", encoding="utf-8")
        with mock.patch.object(vlog, "APP_LOG", str(p), create=True):
            out = feedback._tail_log(lines=2)
        self.assertLess(len(out), 300 * 1024)   # 没有整份读进内存
        self.assertIn("最后一行", out)


class DisguiseHonesty(unittest.TestCase):
    """P2：评分去重、探测失败落 unknown、还原如实报告。"""

    def test_utc8_counted_once(self):
        fp = {"tz_id": "China Standard Time", "locale": "zh-CN",
              "ui_culture": "zh-CN", "cn_fonts": []}
        with mock.patch.object(disguise, "_tz_offset_hours", return_value=8.0):
            sc = disguise.score(fp)
        utc8_hits = [h for h in sc["hits"] if "UTC+8" in h["item"]]
        self.assertEqual(len(utc8_hits), 1)     # 同一判据只算一次
        self.assertEqual(sc["score"], 30 + 24)  # 时区 30 + 中文 24

    def test_probe_failure_is_unknown(self):
        fp = {"tz_id": "", "locale": "en-US", "cn_fonts": []}
        with mock.patch.object(disguise, "_tz_offset_hours", return_value=None):
            sc = disguise.score(fp)
        self.assertEqual(sc["grade"], "unknown")
        self.assertFalse(sc["known"])

    def test_restore_without_backup_not_success(self):
        with mock.patch.object(disguise, "_load_backup", return_value=None):
            ok, msg = disguise.restore_timezone()
        self.assertFalse(ok)
        self.assertIn("未做任何修改", msg)


class GeoLinkCache(unittest.TestCase):
    """P2：解析失败不缓存空结果。"""

    def test_failure_not_cached(self):
        geolink.clear_cache()
        calls = []

        def fake(q):
            calls.append(q)
            return None

        with mock.patch.object(geolink, "_geo", side_effect=fake):
            self.assertIsNone(geolink.locate("9.9.9.9"))
            self.assertIsNone(geolink.locate("9.9.9.9"))
        self.assertEqual(len(calls), 2)     # 失败不写缓存，两次都真的去查


class GithubGitEnv(unittest.TestCase):
    """P2：git 子进程必须禁止交互式凭据，否则后台线程会被弹窗挂死。"""

    def test_no_prompt_env(self):
        ops = github_ops.GithubOps(types.SimpleNamespace(settings={"mixed_port": 7891}))
        env = ops.git_env(7891)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        self.assertNotIn("http_proxy", env)


class AutoStartCommand(unittest.TestCase):
    """P2：自启优先无窗口解释器，命令行必须整体加引号。"""

    def test_pythonw_preferred(self):
        p = autostart._pythonw()
        self.assertTrue(str(p).lower().endswith("pythonw.exe") or p == Path(sys.executable))

    def test_command_is_quoted(self):
        cmd, target = autostart._command()
        self.assertTrue(cmd.startswith(chr(34)))
        self.assertTrue(Path(target).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
