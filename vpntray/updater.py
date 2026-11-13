"""从 GitHub Releases 检查并安装新版本。

设计取舍：
- **只认 GitHub 域名**。下载地址来自 API 返回，但依然要校验，否则一个被污染的
  release 就能让程序去任意主机拉 exe 覆盖自己。
- **替换交给外部 bat**。onedir 打包后主进程正占着 VPNTray.exe 和 _internal 里
  几百个文件，运行中覆盖自己一定失败。所以下载解压都做完，最后写一个等待脚本
  丢给系统执行，主进程正常退出（这样 mihomo 和系统代理才会被清干净）。
- **镜像覆盖但保住 data/，并留一份回滚**。robocopy /MIR 会把新包里没有的旧文件
  删掉（只覆盖不删的话，换过一版以后残留的旧模块还会被加载，出问题极难排查）；
  data/ 用 /XD 排除在外，订阅、设置、日志、纯净度缓存都不参与镜像。覆盖前先把
  旧版本整份镜像到 data/update/rollback，镜像失败就从那里恢复，用户不会面对一个
  打不开的软件。
- **没有 SHA256 的包一律不装**。哈希取不到时自动更新直接拒绝：缺哈希等于允许
  任何人中途换一个 exe 上来。只有用户手动确认（confirm=True）才继续。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

from . import paths
from .logger import get as _get_logger

log = _get_logger("update")

# 只允许 GitHub 自家域名，其余一律拒绝。release 资产的 302 目标换过好几次：
# 早期是 objects.githubusercontent.com，2024 起改成 release-assets.githubusercontent.com
# （旧域名 github-releases.githubusercontent.com 仍可能出现在老 release 上）。
# 少写一个的后果不是「不安全」，而是 _check_host 在跟第二跳时直接抛
# 「拒绝访问非 GitHub 地址」，于是检查更新永远报「最新版没挂 zip」——
# 3.0.1 / 3.0.2 都踩了这个坑。新增 CDN 域名时要一起加进来。
ALLOWED_HOSTS = {"github.com", "api.github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com",
                 "github-releases.githubusercontent.com",
                 "codeload.github.com", "raw.githubusercontent.com"}

MAX_ZIP_BYTES = 300 * 1024 * 1024     # 发布包 70MB 上下，留足余量
MAX_JSON_BYTES = 4 * 1024 * 1024
# 解压后的字节预算。只卡条目数挡不住 zip 炸弹：单个条目声明的 file_size
# 就能是几十 GB，真写下去磁盘直接满，托盘程序表现为「更新卡死」。
MAX_UNPACKED_BYTES = 1200 * 1024 * 1024
MAX_FILE_UNPACKED = 400 * 1024 * 1024

# 所有网络请求共用的超时：GitHub 在国内要借内核的代理口出去，卡住时必须
# 自己断开，不能让一次「检查更新」把后台线程长期占住。
NET_TIMEOUT = 12.0
MAX_REDIRECTS = 5

# owner/name 只允许 GitHub 的合法字符，禁止斜杠以外的路径、空格、协议头。
# 用户从设置页手填时最容易写成 https://github.com/owner/name 或 owner/name/ ，
# 这里统一削掉，免得检查更新直接报 404 让人以为「功能坏了」。
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")


def normalize_repo(raw: Any) -> tuple[bool, str, str]:
    """把用户填的仓库串收敛成 owner/name。返回 (ok, cleaned, error)。"""
    s = str(raw or "").strip()
    if not s:
        return False, "", "仓库为空（留空=不检查更新）"
    s = s.replace("\\", "/")
    low = s.lower()
    for pre in ("https://", "http://"):
        if low.startswith(pre):
            s = s[len(pre):]
            low = s.lower()
            break
    if low.startswith("github.com/"):
        s = s[len("github.com/"):]
    s = s.strip("/")
    if s.lower().endswith(".git"):
        s = s[:-4]
    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        return False, s, "要写成 owner/name，例如 ayuanjc/VPNTray"
    s = parts[0] + "/" + parts[1]
    if not REPO_RE.match(s):
        return False, s, "owner/name 里有非法字符"
    return True, s, ""

# 以前 REPO_RE 在这里被重新定义过一次：两份规则不一样（第一份允许 - 或 . 开头），
# 后者把前者盖掉了，看代码时永远只改到「看起来在用」的那个。只留上面一份。
# tag 常带后缀：v1.8.0-beta、1.8.0+build7、v2.0.0-rc1。后缀不参与比较，
# 否则一个 -beta 后缀就能让「有没有新版本」整个判定失效。
VER_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+~][0-9A-Za-z.\-+]*)?$")

UA = "VPNTray-Updater"

# 域名抽成常量，测试时能整体指向本地假服务（生产永远是这两个）
GH = "https://github.com"
API = "https://api.github.com"


class UpdateError(Exception):
    """更新链路上的可预期失败，文案直接给用户看。"""


# ────────────────────────── 版本号 ──────────────────────────

def parse_version(text: str) -> tuple[int, ...] | None:
    """'v1.8.0' / '1.8' -> (1,8,0) / (1,8)。解析不出来返回 None。"""
    m = VER_RE.match(str(text or "").strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def _pad(v: tuple[int, ...], n: int) -> tuple[int, ...]:
    return v + (0,) * max(0, n - len(v))


def is_newer(latest: str, current: str) -> bool:
    """1.8 > 1.7.1、1.7.1 > 1.7，1.7.0 == 1.7。都解析不出来时一律当没更新。"""
    a, b = parse_version(latest), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return _pad(a, n) > _pad(b, n)


# ────────────────────────── 网络 ──────────────────────────

def _proxies(local_port: int | None) -> dict[str, str] | None:
    """有内核就借它的口出去；GitHub 在国内直连经常不通。"""
    if not local_port:
        return None
    p = f"http://127.0.0.1:{int(local_port)}"
    return {"http": p, "https": p}


def _check_host(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise UpdateError(f"拒绝访问非 GitHub 地址：{host or url}")


def _request(url: str, proxies: dict[str, str] | None, timeout: float,
             method: str = "GET") -> requests.Response:
    """手动跟重定向的请求：每一跳都过一遍 GitHub 白名单。

    以前只校验首跳域名，然后 allow_redirects=True 让 requests 自己跟：一个被污染
    的 302 就能把请求带去任意主机（顺走 token，或者直接下一个 exe 回来覆盖自己）。
    顺带在这里做重试：只对连接层的瞬时故障重试 2 次，指数退避。
    """
    cur = url
    hops = 0
    while True:
        _check_host(cur)
        r = None
        for attempt in range(3):
            try:
                r = requests.request(
                    method, cur,
                    headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
                    proxies=proxies, timeout=timeout, allow_redirects=False, stream=True,
                )
                break
            except requests.RequestException:
                # 403/404 之类的业务错误交给调用方判断，这里只兜连接抖动
                if attempt >= 2:
                    raise
                time.sleep(0.6 * (2 ** attempt))
        if r is None:      # 理论上到不了，兜底免得 mypy/人读代码时猜
            raise UpdateError("请求未能发出")
        loc = r.headers.get("Location") or ""
        if r.status_code in (301, 302, 303, 307, 308) and loc:
            r.close()
            hops += 1
            if hops > MAX_REDIRECTS:
                raise UpdateError("GitHub 重定向次数过多")
            cur = urljoin(cur, loc)
            continue
        return r


def _get(url: str, proxies: dict[str, str] | None, timeout: float = NET_TIMEOUT) -> requests.Response:
    r = _request(url, proxies, timeout)
    if r.status_code == 404:
        r.close()
        raise UpdateError("仓库或 Release 不存在（404）")
    if r.status_code != 200:
        code = r.status_code
        r.close()
        raise UpdateError(f"GitHub 返回 HTTP {code}")
    return r


def check(repo: str, current: str, local_port: int | None = None,
          timeout: float = 12.0) -> dict[str, Any]:
    """查最新 release。异常一律转成 ok=False + error，不往上抛。

    刻意**不走 api.github.com**：未认证的 API 只有 60 次/小时，而机场出口是
    共享 IP，别人跑几次就把额度用光了（实测 startsWith 就是 403
    "API rate limit exceeded"）。改走两样不限流的东西：
      - `/releases/latest` 的 302 Location → 拿到 tag
      - `/releases.atom` → 拿到更新说明
    下载地址按发布约定拼，拼不出来才退回 API。
    """
    repo = (repo or "").strip()
    if not REPO_RE.match(repo):
        return {"ok": False, "error": f"仓库名不合法：{repo or '(空)'}（应形如 owner/name）"}

    proxies = _proxies(local_port)
    tag, err = _latest_tag(repo, proxies, timeout)
    if err:
        return {"ok": False, "error": err}
    parsed = parse_version(tag)
    if not parsed:
        return {"ok": False, "error": f"Release 的 tag 不是版本号：{tag or '(空)'}"}
    latest = ".".join(str(x) for x in parsed)

    zip_url = (f"{GH}/{repo}/releases/download/{quote(tag, safe='')}"
               f"/VPNTray-{latest}-win64.zip")
    size = _head_size(zip_url, proxies, timeout)
    page = f"{GH}/{repo}/releases/tag/{quote(tag, safe='')}"
    sha = ""

    if size == 0:
        # 约定名不存在（手动发布的包名不统一）：退回 API 找资产
        fallback = _api_release(repo, proxies, timeout)
        if fallback:
            zip_url = fallback.get("zip_url") or zip_url
            size = fallback.get("zip_size") or 0
            page = fallback.get("page") or page
            sha = str(fallback.get("zip_sha256") or "").lower()
        if not size:
            return {"ok": True, "has_update": False, "current": current,
                    "latest": latest, "tag": tag,
                    "error": f"最新版 {tag} 没挂 VPNTray-{latest}-win64.zip",
                    "notes": "", "page": page, "zip_url": "", "zip_size": 0,
                    "zip_sha256": "", "published": ""}
    if size < 0:
        size = 0      # 资产在，但服务端没给 Content-Length（chunked）；大小只用于显示

    # is_newer 做的是数字分段比较：3.0.10 > 3.0.9 成立，而服务器给的版本比本地
    # 低（回滚过一版）时永远是 False —— 不能把「降级」当成「有更新」推给用户。
    has_update = is_newer(latest, current)
    if has_update and not sha:
        # 自动更新必须有哈希；这里就把发布方给的哈希找出来，找不到让前端直接
        # 看到原因，而不是等到 prepare 下载完 70MB 才拒。
        sha = _resolve_zip_sha(repo, tag, zip_url, proxies, timeout)
    if has_update and not sha:
        # 最后一条路：API 资产的 digest（可能撞 60 次/时限流，所以放到最后）
        api = _api_release(repo, proxies, timeout)
        if api and str(api.get("zip_url") or "") == zip_url:
            sha = str(api.get("zip_sha256") or "").lower()

    return {
        "ok": True,
        "has_update": has_update,
        "current": current,
        "latest": latest,
        "tag": tag,
        "notes": _atom_notes(repo, tag, proxies, timeout),
        "page": page,
        "zip_url": zip_url,
        "zip_size": size,
        # 空串 = 发布方没提供哈希。前端可以照常提示有新版本，但 prepare 会拒绝
        # 自动安装（缺哈希等于没有完整性校验）。
        "zip_sha256": sha,
        "published": "",
    }


def _req_raw(url: str, proxies: dict[str, str] | None, timeout: float,
             method: str = "GET", allow_redirects: bool = True) -> requests.Response:
    """带 github 白名单的裸请求：只校验主机，不校验状态码。

    和 _get 的区别：_get 是给下载用的（必须 200 且校验大小），这里是给
    「看 302」和「读 header」用的，非 200 也是有效信息。
    """
    if allow_redirects:
        return _request(url, proxies, timeout, method=method)
    _check_host(url)
    return requests.request(
        method, url,
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
        proxies=proxies, timeout=timeout, allow_redirects=False, stream=True,
    )


def _latest_tag(repo: str, proxies: dict[str, str] | None,
                timeout: float) -> tuple[str, str]:
    """`/releases/latest` 会 302 到 `/releases/tag/<tag>`，从 Location 里抠 tag。"""
    url = f"{GH}/{repo}/releases/latest"
    try:
        r = _req_raw(url, proxies, timeout, allow_redirects=False)
        try:
            code = r.status_code
            loc = r.headers.get("Location") or ""
        finally:
            r.close()
    except UpdateError as e:
        return "", str(e)
    except requests.RequestException as e:
        return "", f"连不上 GitHub：{type(e).__name__}"

    if code == 404:
        return "", "仓库不存在，或者还没有发布过任何版本"
    if code not in (301, 302, 303, 307, 308):
        return "", f"GitHub 返回 HTTP {code}"
    m = re.search(r"/releases/tag/([^/?#]+)", urlparse(loc).path)
    if not m:
        return "", "解析不出最新版本号（GitHub 页面结构变了？）"
    return unquote(m.group(1)), ""


def _head_size(url: str, proxies: dict[str, str] | None, timeout: float) -> int:
    """返回字节数。0 = 拿不到/不存在，-1 = 存在但不知道大小。

    GitHub 的 release 资产是 302 到 objects.githubusercontent.com，
    那边经常回 Transfer-Encoding: chunked（没有 Content-Length）。
    早期版本把「没有 Content-Length」当成「资产不存在」，结果一出新版本
    自动更新就报「没挂 VPNTray-x-win64.zip」——大小只是用来显示的，不能当存在性判据。
    """
    # 先打 HEAD：只是想知道大小，没必要把整个 zip 拉下来。
    # 有的 CDN 对 HEAD 回 403/405，那种情况再退回 GET（stream=True，读完 header 就关）。
    for method in ("HEAD", "GET"):
        try:
            r = _req_raw(url, proxies, timeout, method=method)
        except (UpdateError, requests.RequestException):
            if method == "HEAD":
                continue
            return 0
        try:
            if r.status_code in (403, 405) and method == "HEAD":
                continue
            if r.status_code != 200:
                return 0
            raw = r.headers.get("Content-Length")
            if not raw:
                return -1
            try:
                return int(raw)
            except (TypeError, ValueError):
                return -1
        finally:
            r.close()
    return 0


def _html_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _atom_notes(repo: str, tag: str, proxies: dict[str, str] | None,
                timeout: float) -> str:
    """从 releases.atom 取这一版的更新说明（不限流）。失败就返回空串。"""
    url = f"{GH}/{repo}/releases.atom"
    try:
        r = _req_raw(url, proxies, timeout)
        try:
            raw = r.raw.read(MAX_JSON_BYTES + 1, decode_content=True)
        finally:
            r.close()
    except (UpdateError, requests.RequestException, OSError):
        return ""
    if len(raw) > MAX_JSON_BYTES:
        return ""
    try:
        root = ET.fromstring(raw.decode("utf-8", "replace"))
    except ET.ParseError:
        return ""
    ns = "{http://www.w3.org/2005/Atom}"
    want = tag.lstrip("vV")
    # Release 标题不一定正好是 tag：2.3.0 起都写成「VPNTray 2.3.0 — 一句话」，
    # 早期严格相等比对导致更新弹窗里永远没有更新说明。改成「标题里出现这个版本号」，
    # 用 \b 卡住边界，避免 2.4.0 命中 2.4.0-rc1 或 12.4.0。
    want_re = re.compile(rf"(?<![0-9A-Za-z.])v?{re.escape(want)}(?![0-9A-Za-z.])", re.I)
    for entry in root.findall(f"{ns}entry"):
        title = (entry.findtext(f"{ns}title") or "").strip()
        if title.lstrip("vV") != want and not want_re.search(title):
            continue
        node = entry.find(f"{ns}content")
        body = (node.text or "") if node is not None else ""
        return _html_text(body)[:4000]
    return ""


def _api_release(repo: str, proxies: dict[str, str] | None,
                 timeout: float) -> dict[str, Any] | None:
    """兜底：只在这个 release 的资产名不符合约定时才用，可能撞上 60 次/时限流。"""
    url = f"{API}/repos/{repo}/releases/latest"
    try:
        r = _req_raw(url, proxies, timeout)
        try:
            if r.status_code != 200:
                return None
            raw = r.raw.read(MAX_JSON_BYTES + 1, decode_content=True)
        finally:
            r.close()
        data = json.loads(raw.decode("utf-8", "replace"))
    except (UpdateError, requests.RequestException, ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
    picked = zips[0] if len(zips) == 1 else None
    if picked is None:
        return None
    try:
        size = int(picked.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    sha = str(picked.get("digest") or "")
    return {
        "zip_url": str(picked.get("browser_download_url") or ""),
        "zip_size": size,
        "zip_sha256": sha[7:] if sha.startswith("sha256:") else "",
        "page": str(data.get("html_url") or ""),
    }


# `sha256sum` 的输出：<hex>  <name>；单文件 .sha256 里可能只有哈希
_SHA_LINE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def _fetch_text(url: str, proxies: dict[str, str] | None, timeout: float,
                limit: int = 64 * 1024) -> str:
    """取一小段文本（.sha256 / SHA256SUMS.txt）。取不到返回空串，不抛。"""
    try:
        r = _req_raw(url, proxies, timeout)
        try:
            if r.status_code != 200:
                return ""
            raw = r.raw.read(limit + 1, decode_content=True)
        finally:
            r.close()
    except (UpdateError, requests.RequestException, OSError):
        return ""
    if len(raw) > limit:
        return ""
    return raw.decode("utf-8", "replace")


def _sha_from_text(text: str, name: str) -> str:
    """从 sha256sum 格式里挑出 name 这一行的哈希。

    发布脚本写的是 `hash  VPNTray.exe` 这种「哈希 + 文件名」，不同人手工上传时
    文件名可能带路径，所以按 basename 比。整份文件只有一个哈希且没写文件名时
    （单文件 .sha256）也认。
    """
    want = unquote(name or "").lower()
    fallback = ""
    for line in (text or "").splitlines():
        m = _SHA_LINE.search(line)
        if not m:
            continue
        got = m.group(1).lower()
        rest = line[m.end():].strip().lstrip("*").strip()
        if not rest:
            # 只有哈希：单文件摘要，先记着，后面没有匹配到文件名就用它
            fallback = fallback or got
            continue
        if rest.replace("\\", "/").rsplit("/", 1)[-1].lower() == want:
            return got
    return fallback


def _resolve_zip_sha(repo: str, tag: str, zip_url: str,
                    proxies: dict[str, str] | None, timeout: float) -> str:
    """找发布包自己的 SHA256：`<zip>.sha256` → `SHA256SUMS.txt` → 空串。

    release.ps1 的 SHA256SUMS.txt 只写 VPNTray.exe 和 mihomo.exe（包里的文件），
    zip 自身的哈希写在 release/<zip>.sha256 里、由发布者贴到 release notes。所以
    两种都试；只在真的匹配到 zip 文件名时才采信，避免把 exe 的哈希当成 zip 的。
    """
    name = zip_url.rsplit("/", 1)[-1]
    base = f"{GH}/{repo}/releases/download/{quote(tag, safe='')}"
    for url in (f"{zip_url}.sha256", f"{base}/SHA256SUMS.txt"):
        got = _sha_from_text(_fetch_text(url, proxies, timeout), name)
        if got:
            log.info("更新包 SHA256 取自 %s", url)
            return got
    return ""


def download(url: str, dest: Path, local_port: int | None = None,
             on_progress: Callable[[int, int], None] | None = None,
             timeout: float = NET_TIMEOUT) -> Path:
    """下载 zip 到 dest，返回 dest。超过上限直接中断。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    proxies = _proxies(local_port)
    got = 0
    try:
        r = _get(url, proxies, timeout)
        try:
            total = int(r.headers.get("Content-Length") or 0)
            if total > MAX_ZIP_BYTES:
                raise UpdateError(f"安装包太大：{total / 1048576:.0f} MB")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    got += len(chunk)
                    if got > MAX_ZIP_BYTES:
                        raise UpdateError("安装包超过大小上限，已中止")
                    f.write(chunk)
                    if on_progress:
                        on_progress(got, total)
        finally:
            r.close()
    except requests.RequestException as e:
        tmp.unlink(missing_ok=True)
        raise UpdateError(f"下载失败：{type(e).__name__}") from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if got == 0:
        tmp.unlink(missing_ok=True)
        raise UpdateError("下载到 0 字节")
    tmp.replace(dest)
    return dest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# ────────────────────────── 解包与校验 ──────────────────────────

def extract(zip_path: Path, workdir: Path, expect_version: str) -> Path:
    """解压到 workdir/payload，返回含 VPNTray.exe 的那层目录。

    只解压普通文件，拒绝绝对路径和 ..（zip slip）。解完再验一遍版本号，
    免得把一个包名对、内容错的 zip 装上去。
    """
    workdir = Path(workdir)
    payload = workdir / "payload"
    if payload.exists():
        shutil.rmtree(payload, ignore_errors=True)
    payload.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if len(infos) > 20000:
                raise UpdateError("安装包内文件数异常")
            # 声明大小之和先卡一道：真实的炸弹包在声明上就已经超了
            declared = sum(max(0, i.file_size) for i in infos)
            if declared > MAX_UNPACKED_BYTES:
                raise UpdateError(f"安装包解压后过大：{declared / 1048576:.0f} MB")
            root_abs = payload.resolve()
            for info in infos:
                raw = info.filename.replace("\\", "/")
                if raw.startswith("/") or ".." in raw.split("/"):
                    raise UpdateError(f"安装包内有可疑路径：{raw[:80]}")
                # 字符串 startswith 会放过 `payload-evil/` 这种同前缀目录，用
                # commonpath 比真实路径；跨盘符时 commonpath 抛 ValueError，也算越界。
                target = (payload / raw).resolve()
                if not _inside(target, root_abs):
                    raise UpdateError("安装包路径越界")
                if info.file_size > MAX_FILE_UNPACKED:
                    raise UpdateError(f"安装包内单个文件过大：{raw[:60]}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # 声明值不可信，写出时再数一遍实际字节
                written = 0
                with zf.open(info) as src, open(target, "wb") as dst:
                    for chunk in iter(lambda: src.read(256 * 1024), b""):
                        written += len(chunk)
                        if written > MAX_FILE_UNPACKED:
                            raise UpdateError(f"安装包内文件实际大小超限：{raw[:60]}")
                        dst.write(chunk)
    except zipfile.BadZipFile as e:
        raise UpdateError(f"安装包不是有效的 zip：{e}") from e

    root = find_exe_root(payload)
    if root is None:
        raise UpdateError("安装包里找不到 VPNTray.exe")

    # 版本自证：发布包里 assets/index.html 带着 UI_VER（onedir 打包后它在 _internal/assets）
    idx = root / "assets" / "index.html"
    if not idx.exists():
        idx = root / "_internal" / "assets" / "index.html"
    if idx.exists():
        try:
            text = idx.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = re.search(r"const\s+UI_VER\s*=\s*['\"]([^'\"]+)['\"]", text)
        found = m.group(1) if m else ""
        if found:
            # 以前只比主版本前缀：3.0.9 的包能被当成 3.0.10 装上，装完还是旧
            # 版本，界面却显示已是最新。改成逐段数字比较（补 0 对齐）。
            fv, ev = parse_version(found), parse_version(expect_version)
            if fv is None or ev is None:
                raise UpdateError(f"安装包版本号无法解析：包里是 {found}")
            width = max(len(fv), len(ev))
            if _pad(fv, width) != _pad(ev, width):
                raise UpdateError(f"安装包版本对不上：期望 {expect_version}，包里是 {found}")
    return root


def _inside(path: Path, root: Path) -> bool:
    """path 是否在 root 之内（两者都已 resolve）。"""
    try:
        return os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        # 不同盘符（C: / D:）commonpath 直接抛，这种情况一定是越界
        return False


def find_exe_root(base: Path, exe_name: str = "VPNTray.exe") -> Path | None:
    """zip 里通常有一层 VPNTray-1.8.0/，也可能被重新打包成平铺结构。"""
    if (base / exe_name).exists():
        return base
    for child in sorted(base.iterdir() if base.is_dir() else []):
        if child.is_dir() and (child / exe_name).exists():
            return child
    return None


# ────────────────────────── 应用 ──────────────────────────

BAT = """@echo off
chcp 65001 >nul
setlocal EnableExtensions
rem 路径一律由 launch() 通过环境变量注入：以前是把路径 format 进 .cmd 源码，
rem 安装目录里只要出现 & % ^ 或空格，cmd 解析到那一行就会把命令拆开（把程序装在
rem D:\\VPN & Tools\\ 这种目录下时更新必然失败）。环境变量在 set "X=%Y%" 的
rem 引号里展开，这些字符都不会被当成控制符。
set "TARGET=%VPNTRAY_TARGET%"
set "PAYLOAD=%VPNTRAY_PAYLOAD%"
set "EXE=%VPNTRAY_EXE%"
set "WORKDIR=%VPNTRAY_WORKDIR%"
set "ROLLBACK=%VPNTRAY_ROLLBACK%"
rem log 写 TEMP：workdir 里的 payload 会被删掉
set "LOG=%TEMP%\\vpntray-update.log"
echo [%DATE% %TIME%] ==== start ==== > "%LOG%"
echo target=%TARGET% >> "%LOG%"
echo payload=%PAYLOAD% >> "%LOG%"
if "%TARGET%"=="" goto noenv
if not exist "%PAYLOAD%\\%EXE%" goto noenv
rem 等上一份进程退出 + 让系统释放文件句柄。
rem 刻意不用 tasklist：update.cmd 是用 DETACHED_PROCESS 起的，没有控制台，
rem tasklist 在那种环境里会卡死（实测纯 for+ping 正常，加 tasklist 一行输出都没有；
rem 2.0.2 之前所有人卡在「更新器已启动」之后就没动静，就是这个原因）。
rem 主进程那边是 os._exit(0)，进程立刻消失，固定等 3 秒足够；文件还被占着就交给
rem robocopy 自己重试（/R:10 /W:1 = 最多 10 次、每次隔 1 秒）。
ping -n 4 127.0.0.1 >nul
echo backup >> "%LOG%"
rem 覆盖前先整份备份旧版本（跳过 data：那是用户数据）。备份失败不致命，只是
rem 覆盖失败时没有回滚点，最多让用户重下一份安装包。
if exist "%ROLLBACK%" rmdir /s /q "%ROLLBACK%"
robocopy "%TARGET%" "%ROLLBACK%" /E /XD "%TARGET%\\data" /R:2 /W:1 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1
echo backup rc=%ERRORLEVEL% >> "%LOG%"
echo copying >> "%LOG%"
rem /MIR：新包里没有的旧文件要删掉。只覆盖不删的话，换过一版以后残留的旧模块、
rem 旧 DLL 还会被加载，出问题极难查。
rem /XD data：用户数据不参与镜像 —— 订阅、设置、日志、纯净度缓存都在 data 下。
robocopy "%PAYLOAD%" "%TARGET%" /MIR /XD "%TARGET%\\data" /R:10 /W:1 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo robocopy rc=%RC% >> "%LOG%"
rem robocopy 的 0-7 都算成功（1=复制了文件，2=多出文件，3=复制+多出），8 以上才是失败
if %RC% GEQ 8 goto restore
start "" "%TARGET%\\%EXE%"
echo started new version >> "%LOG%"
rem 只清 payload（几百 MB）。rollback 留着：万一手抖更新到坏版本还能手动回滚。
if exist "%WORKDIR%\\payload" rmdir /s /q "%WORKDIR%\\payload"
(goto) 2>nul & del "%~f0"
exit /b 0
:restore
rem 覆盖失败：从备份镜像回去，再把旧版本拉起来，别让用户面对一个打不开的软件
echo restore >> "%LOG%"
if exist "%ROLLBACK%" robocopy "%ROLLBACK%" "%TARGET%" /MIR /XD "%TARGET%\\data" /R:10 /W:1 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1
start "" "%TARGET%\\%EXE%"
echo copy failed, restored from rollback >> "%LOG%"
(goto) 2>nul & del "%~f0"
exit /b 1
:noenv
rem 路径清单没拿到（被清理软件删了/没写成功）：什么都不动，只把旧版本拉起来
echo missing VPNTRAY_TARGET / VPNTRAY_PAYLOAD >> "%LOG%"
if not "%TARGET%"=="" start "" "%TARGET%\\%EXE%"
(goto) 2>nul & del "%~f0"
exit /b 2
"""


def stage(payload_root: Path, workdir: Path, target_root: Path | None = None,
          exe_name: str = "VPNTray.exe") -> Path:
    """写好 update.cmd（附一份路径清单）并返回它。调用方随后应让应用正常退出。

    路径不写进 .cmd 源码，而是落在 update.json 里、由 launch() 当环境变量传进去：
    cmd 对没转义的 & ^ % 没有容错，而安装目录是用户自己挑的。
    """
    target = Path(target_root or paths.ROOT).resolve()
    payload = Path(payload_root).resolve()
    workdir = Path(workdir).resolve()
    if not (payload / exe_name).exists():
        raise UpdateError("待安装目录里没有 VPNTray.exe")
    workdir.mkdir(parents=True, exist_ok=True)   # 正常由 prepare 建好，这里兜底
    plan = {
        "target": str(target),
        "payload": str(payload),
        "exe": str(exe_name),
        "workdir": str(workdir),
        "rollback": str(workdir / "rollback"),
    }
    try:
        (workdir / "update.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        raise UpdateError(f"更新脚本写不出来：{e}") from e
    bat = workdir / "update.cmd"
    # 源码里是 LF，cmd 只认 CRLF；两个方向先归一，免得写出 \r\r\n
    bat.write_bytes(BAT.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))
    return bat


def launch(bat: Path) -> None:
    """脱离父进程启动更新脚本，主进程死了它也继续跑。"""
    bat = Path(bat)
    if not bat.exists():
        raise UpdateError("更新脚本没写成功")
    # 路径清单是 stage() 写好的，这里只塞进环境变量：.cmd 全程不解析路径内容
    plan_path = bat.parent / "update.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UpdateError(f"更新脚本的路径清单读不到：{type(e).__name__}") from e
    if not isinstance(plan, dict) or not plan.get("target") or not plan.get("payload"):
        raise UpdateError("更新脚本的路径清单不完整")
    env = dict(os.environ)
    env.update({
        "VPNTRAY_TARGET": str(plan.get("target") or ""),
        "VPNTRAY_PAYLOAD": str(plan.get("payload") or ""),
        "VPNTRAY_EXE": str(plan.get("exe") or "VPNTray.exe"),
        "VPNTRAY_WORKDIR": str(plan.get("workdir") or plan_path.parent),
        "VPNTRAY_ROLLBACK": str(plan.get("rollback") or (plan_path.parent / "rollback")),
    })
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        cwd=str(bat.parent),
        env=env,
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ────────────────────────── 一站式 ──────────────────────────

def update_workdir() -> Path:
    return paths.DATA / "update"


def prepare(info: dict[str, Any], local_port: int | None = None,
            on_progress: Callable[[int, int], None] | None = None,
            confirm: bool = False) -> Path:
    """下载 → 校验 → 解压，返回 payload 根。不重启、不动当前文件。

    confirm 只影响「拿不到 SHA256」这一种情况：缺哈希等于没有完整性校验，中途被
    换包完全看不出来，所以自动更新一律拒绝；用户在界面上明确点过「仍然安装」之后
    传 confirm=True 才继续，并把这件事写进日志。
    """
    url = str(info.get("zip_url") or "")
    latest = str(info.get("latest") or "")
    if not url:
        raise UpdateError("这个 Release 没挂 windows 安装包")
    if not parse_version(latest):
        raise UpdateError("版本号不可用")
    if not os.access(paths.DATA, os.W_OK):
        raise UpdateError("data 目录不可写，无法解压安装包")

    work = update_workdir()
    work.mkdir(parents=True, exist_ok=True)
    zip_path = work / f"VPNTray-{latest}.zip"
    proxies = _proxies(local_port)

    want = str(info.get("zip_sha256") or "").strip().lower()
    if not want:
        # check() 已经找过一轮；这里再补一次，防止调用方手工拼 info 时漏了哈希
        want = _resolve_zip_sha(str(info.get("repo") or ""),
                               str(info.get("tag") or latest), url, proxies, NET_TIMEOUT)
    if not want and not confirm:
        # 文案要和界面上的「已跳过自动更新」对上，用户得知道为什么没装
        raise UpdateError("无法校验更新包（缺 SHA256），已跳过自动更新")

    download(url, zip_path, local_port=local_port, on_progress=on_progress)

    if want:
        got = sha256_of(zip_path)
        if got != want:
            zip_path.unlink(missing_ok=True)
            raise UpdateError("安装包校验和不匹配，已丢弃")
    else:
        log.warning("用户已确认在没有 SHA256 的情况下安装 %s：完整性无法验证", latest)

    return extract(zip_path, work, latest)


__all__ = [
    "UpdateError", "parse_version", "is_newer", "check", "download", "sha256_of",
    "extract", "find_exe_root", "stage", "launch", "prepare", "update_workdir",
]
