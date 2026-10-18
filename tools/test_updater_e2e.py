"""自动更新全链路自测：本地起一个假 GitHub，把 check → download → extract → stage 走通。

不碰真实仓库、不覆盖任何文件：只验证
  1) `releases/latest` 的 302 能取到 tag
  2) 按约定拼出来的下载地址能下
  3) 版本自证（包里 UI_VER 对不上就拒绝）
  4) 生成的 update.cmd 内容正确
  5) 非 GitHub 域名一律拒

跑法：python tools/test_updater_e2e.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vpntray import updater  # noqa: E402

REPO = "ayuanjc/VPNTray"
NEW_VER = "1.9.0"
NEW_TAG = "v1.9.0"
ZIP_NAME = f"VPNTray-{NEW_VER}-win64.zip"

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>v1.9.0</title>
    <content type="html">&lt;h3&gt;更新内容&lt;/h3&gt;&lt;ul&gt;
      &lt;li&gt;修了 a 的 bug&lt;/li&gt;
      &lt;li&gt;优化 b 的速度&lt;/li&gt;
    &lt;/ul&gt;</content>
  </entry>
</feed>
"""


def build_zip(ui_ver: str = NEW_VER) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        base = f"VPNTray-{NEW_VER}"
        zf.writestr(f"{base}/VPNTray.exe", b"MZ fake exe " + b"\0" * 2048)
        zf.writestr(f"{base}/assets/index.html",
                    f"<html><script>const UI_VER = '{ui_ver}';</script></html>")
        zf.writestr(f"{base}/README.md", "# VPN Tray")
    return buf.getvalue()


class FakeGitHub(BaseHTTPRequestHandler):
    ui_ver = NEW_VER

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        port = self.server.server_port

        if path == f"/{REPO}/releases/latest":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/{REPO}/releases/tag/{NEW_TAG}")
            self.end_headers()
            return
        if path == f"/{REPO}/releases.atom":
            body = ATOM.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/atom+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == f"/{REPO}/releases/download/{NEW_TAG}/{ZIP_NAME}":
            body = build_zip(FakeGitHub.ui_ver)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main() -> int:
    srv = HTTPServer(("127.0.0.1", 0), FakeGitHub)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 只在这条测试里把本机加进白名单、并把域名指向假服务
    updater.ALLOWED_HOSTS.add("127.0.0.1")
    real_gh, real_api = updater.GH, updater.API
    updater.GH = f"http://127.0.0.1:{port}"
    updater.API = f"http://127.0.0.1:{port}"
    fails: list[str] = []

    try:
        print("=== 1. check：302 取 tag + 约定拼下载地址 + atom 说明 ===")
        info = updater.check(REPO, "1.8.0", local_port=None, timeout=10)
        print("   ", {k: info.get(k) for k in ("ok", "has_update", "latest", "tag", "zip_size")})
        if not (info.get("ok") and info.get("has_update")):
            fails.append(f"check 没识别出新版本：{info}")
        if info.get("latest") != NEW_VER:
            fails.append(f"latest 期望 {NEW_VER}，得到 {info.get('latest')}")
        if info.get("zip_size", 0) <= 0:
            fails.append("拿不到安装包体积")
        if "修了 a 的 bug" not in str(info.get("notes")):
            fails.append(f"atom 说明没解析出来：{info.get('notes')!r}")
        print("    notes =", str(info.get("notes")).replace("\n", " | ")[:90])

        print("\n=== 2. 同版本不该提示更新 ===")
        same = updater.check(REPO, NEW_VER, local_port=None, timeout=10)
        print("    has_update =", same.get("has_update"))
        if same.get("has_update"):
            fails.append("同版本误判为有更新")

        print("\n=== 3. prepare：下载 → 解压 → 找 exe → 版本自证 ===")
        work = Path(tempfile.mkdtemp(prefix="vpntray-upd-"))
        progress: list[tuple[int, int]] = []
        root = None
        try:
            root = updater.prepare(dict(info), local_port=None,
                                   on_progress=lambda d, t: progress.append((d, t)))
        except updater.UpdateError as e:
            fails.append(f"prepare 失败：{e}")
        if root:
            print("    payload root =", root.name)
            print("    exe 存在 =", (root / "VPNTray.exe").exists())
            print("    进度回调 =", progress[:1], "…", progress[-1:] if progress else [])
            if root.name != f"VPNTray-{NEW_VER}":
                fails.append(f"解压根目录名不对：{root.name}")
            if not (root / "assets" / "index.html").exists():
                fails.append("解压后缺 assets/index.html")

        print("\n=== 4. 版本自证：包内 UI_VER 对不上要拒绝 ===")
        bad_zip = work / "bad.zip"
        bad_zip.write_bytes(build_zip(ui_ver="2.5.0"))
        try:
            updater.extract(bad_zip, work / "bad", NEW_VER)
            fails.append("版本对不上的包竟然通过了自证")
        except updater.UpdateError as e:
            print("    正确拒绝：", e)

        print("\n=== 5. stage：生成的 update.cmd 内容 ===")
        if root:
            bat = updater.stage(root, work / "stage", target_root=work / "target")
            text = bat.read_bytes().decode("gbk", "replace")
            print("    bat =", bat.name)
            for kw in ("robocopy", "tasklist", "VPNTray.exe", "/E /IS"):
                ok = kw in text
                print(f"      含 {kw!r}: {ok}")
                if not ok:
                    fails.append(f"update.cmd 缺 {kw}")
            if str(work / "target") not in text:
                fails.append("update.cmd 里的目标目录不对")

        print("\n=== 6. 非 GitHub 域名必须拒 ===")
        for bad in ("http://evil.example.com/x.zip", "https://raw.githack.com/a.zip"):
            try:
                updater._req_raw(bad, None, 2)
                fails.append(f"没拦住 {bad}")
            except updater.UpdateError:
                print("    拒绝：", bad)
    finally:
        updater.GH, updater.API = real_gh, real_api
        srv.shutdown()

    print()
    if fails:
        print("FAIL：")
        for f in fails:
            print("  -", f)
        return 1
    print("全部通过 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
