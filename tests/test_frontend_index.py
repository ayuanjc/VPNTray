"""前端 assets/index.html 的静态约束测试（不启浏览器、不联网，几十毫秒跑完）。

跑法（仓库根目录）：
    python -m pytest tests -q
    python tests/test_frontend_index.py

守的是审查里最容易静默回退的几条：
- 存储型 XSS：场景 meta 拼进 innerHTML 之前必须整段 esc()；
- 请求超时：api() 必须带 AbortController，否则一次挂死的 /api/state 会让轮询永久停摆；
- 轮询旗标必须在 finally 里复位，且带失败退避；
- 界面不能引用外部 CDN / 字体（程序要在离线机器上跑）；
- toast 不能回显完整 token。
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
INDEX = ROOT / "assets" / "index.html"


def _scripts(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


class FrontendStaticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.html = INDEX.read_text(encoding="utf-8")

    def test_inline_scripts_pass_node_check(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node，跳过内联脚本语法检查")
        for i, src in enumerate(_scripts(self.html), 1):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
                fh.write(src)
                tmp = pathlib.Path(fh.name)
            try:
                proc = subprocess.run([node, "--check", str(tmp)], capture_output=True, text=True, timeout=120)
                self.assertEqual(proc.returncode, 0, f"第 {i} 段内联脚本语法错误：{proc.stderr}")
            finally:
                tmp.unlink(missing_ok=True)

    def test_scene_meta_escaped_before_innerhtml(self) -> None:
        # exit 来自订阅里的节点名，可以带 HTML；只要 join 之后不是整体 esc()，
        # 打开「场景」页就会执行任意脚本（同源可调 /api/quit）。
        self.assertIn("esc(meta.join(' · '))", self.html)
        self.assertIsNone(re.search(r"(?<!esc\()meta\.join\(", self.html), "meta.join 必须整体走 esc()")

    def test_api_has_abort_controller(self) -> None:
        self.assertIn("new AbortController()", self.html)
        self.assertIn("E_TIMEOUT", self.html)
        self.assertIn("请求超时，内核可能没响应", self.html)

    def test_refresh_resets_polling_flag_in_finally(self) -> None:
        # 旗标在 await 之前置位，就必须在 finally 复位；否则一次超时 = 面板永久停更。
        m = re.search(r"async function refresh\(\) \{([\s\S]*?)\n\}", self.html)
        self.assertIsNotNone(m, "找不到 refresh()")
        body = m.group(1)
        self.assertIn("polling = true;", body)
        self.assertIn("} finally {", body)
        self.assertIn("polling = false;", body)
        self.assertIn("pollFails += 1;", body)
        self.assertIn("visibilitychange", self.html)
        self.assertIn("POLL_MAX", self.html)

    def test_no_external_assets(self) -> None:
        refs = re.findall(r'(?:src|href)="([^"]+)"', self.html)
        bad = [u for u in refs if u.startswith(("http://", "https://", "//"))]
        self.assertEqual(bad, [], f"发现外部引用：{bad}")

    def test_token_never_echoed_to_toast(self) -> None:
        self.assertNotIn("token 是：", self.html)
        self.assertIn("data/settings.json", self.html)


if __name__ == "__main__":
    unittest.main()
