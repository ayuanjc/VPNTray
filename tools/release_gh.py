"""建 GitHub release v2.0.1 并上传 zip。token 从 git credential 取，不落盘。"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

cred = subprocess.run(
    ["git", "credential", "fill"],
    input="protocol=https\nhost=github.com\n\n",
    capture_output=True, text=True, check=True,
).stdout
tok = ""
for line in cred.splitlines():
    if line.startswith("password="):
        tok = line[9:]
if not tok:
    sys.exit("拿不到 token")

TAG = "v2.0.1"
NAME = "VPNTray 2.0.1"
BODY = """### 修复：点托盘「打开控制台」没反应

2.0.0 有一个真实的坑：从浏览器下载的 zip 解压后，每个文件带一个「来自 Internet」
标记（`Zone.Identifier`）。.NET 拒绝加载带这个标记的程序集，于是 pythonnet 初始化失败、
窗口进程秒退。而当时的代码只判断「子进程有没有被创建」，所以既不回退浏览器、也不报错、
日志里一行都没有 —— 表现为点了完全没反应。

2.0.1 三处一起改：

1. **启动时自动清除标记**：主进程和窗口进程启动时都会扫一遍程序目录，把
   `Zone.Identifier` 删掉。不需要去右键属性勾「解除锁定」。
2. **起窗口后验活**：`spawn()` 会等最多 6 秒边等边检查窗口真的出现没有；子进程秒退就
   返回失败，先清标记重试一次，还不行就**自动回退浏览器**。
3. **失败必须说出来**：窗口进程现在也写日志（原来 `--window` 分支在 logger 初始化
   之前就 return 了）；起不来会弹说明框，托盘也会提示「已改用浏览器打开」。

后台没启动、WebView2 缺失这类情况现在都有明确提示，不再静默。

---

已装 2.0.0 且正常的话不升也行；点托盘没反应就直接覆盖升级，不用手动解除文件锁定。
"""


def api(path, method="GET", body=None, ctype="application/json"):
    url = "https://api.github.com" + path
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


zt = ROOT / "release" / "VPNTray-2.0.1-win64.zip"
if not zt.is_file():
    sys.exit(f"没有 {zt}")

print("查现有 release ...")
try:
    rel = api(f"/repos/ayuanjc/VPNTray/releases/tags/{TAG}")
    print("  已存在:", rel["id"])
except urllib.error.HTTPError as e:
    if e.code != 404:
        raise
    rel = api("/repos/ayuanjc/VPNTray/releases", "POST", {
        "tag_name": TAG, "name": NAME, "body": BODY,
        "draft": False, "prerelease": False,
    })
    print("  已新建:", rel["id"])

# 同名资产先删掉，否则上传 422
for a in rel.get("assets", []):
    if a["name"] == zt.name:
        api(f"/repos/ayuanjc/VPNTray/releases/assets/{a['id']}", "DELETE")
        print("  删掉旧资产:", a["name"])

print(f"上传 {zt.name} ({zt.stat().st_size/1048576:.1f} MB) ...")
up = rel["upload_url"].split("{")[0] + f"?name={zt.name}"
req = urllib.request.Request(up, data=zt.read_bytes(), method="POST")
req.add_header("Authorization", f"Bearer {tok}")
req.add_header("Content-Type", "application/zip")
with urllib.request.urlopen(req, timeout=600) as r:
    a = json.loads(r.read())
print("  完成:", a["name"], a["size"], "bytes")
print("release:", rel["html_url"])
