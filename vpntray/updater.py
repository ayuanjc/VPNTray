"""从 GitHub Releases 检查并安装新版本。

设计取舍：
- **只认 GitHub 域名**。下载地址来自 API 返回，但依然要校验，否则一个被污染的
  release 就能让程序去任意主机拉 exe 覆盖自己。
- **替换交给外部 bat**。onedir 打包后主进程正占着 VPNTray.exe 和 _internal 里
  几百个文件，运行中覆盖自己一定失败。所以下载解压都做完，最后写一个等待脚本
  丢给系统执行，主进程正常退出（这样 mihomo 和系统代理才会被清干净）。
- **覆盖不删除**。robocopy /E 只写同名文件，用户的 data/ 里那些订阅、日志、
  纯净度缓存不会被新包里的 data/purity.json 顺手带走。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from . import paths
from .logger import get as _get_logger

log = _get_logger("update")

# 只允许从这两个域下载，重定向后的 objects.githubusercontent.com 是 GitHub 的
# release 文件 CDN，其余一律拒绝。
ALLOWED_HOSTS = {"github.com", "api.github.com", "objects.githubusercontent.com",
                 "codeload.github.com", "raw.githubusercontent.com"}

MAX_ZIP_BYTES = 300 * 1024 * 1024     # 发布包 70MB 上下，留足余量
MAX_JSON_BYTES = 4 * 1024 * 1024

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9._-]{1,100}$")
# tag 常带后缀：v1.8.0-beta、1.8.0+build7、v2.0.0-rc1。后缀不参与比较，
# 否则一个 -beta 后缀就能让「有没有新版本」整个判定失效。
VER_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+~][0-9A-Za-z.\-+]*)?$")

UA = "VPNTray-Updater"


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


def _get(url: str, proxies: dict[str, str] | None, timeout: float = 12.0) -> requests.Response:
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise UpdateError(f"拒绝从非 GitHub 地址下载：{host or url}")
    r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
                     proxies=proxies, timeout=timeout, stream=True)
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
    """查最新 release。异常一律转成 ok=False + error，不往上抛。"""
    repo = (repo or "").strip()
    if not REPO_RE.match(repo):
        return {"ok": False, "error": f"仓库名不合法：{repo or '(空)'}（应形如 owner/name）"}

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    proxies = _proxies(local_port)
    try:
        r = _get(url, proxies, timeout)
        try:
            raw = r.raw.read(MAX_JSON_BYTES + 1, decode_content=True)
        finally:
            r.close()
        if len(raw) > MAX_JSON_BYTES:
            raise UpdateError("Release 信息异常地大")
        data = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise UpdateError("Release 信息格式不对")
    except UpdateError as e:
        return {"ok": False, "error": str(e)}
    except requests.RequestException as e:
        return {"ok": False, "error": f"连不上 GitHub：{type(e).__name__}"}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": f"解析 Release 失败：{type(e).__name__}"}

    tag = str(data.get("tag_name") or "")
    parsed = parse_version(tag)
    if not parsed:
        return {"ok": False, "error": f"Release 的 tag 不是版本号：{tag or '(空)'}"}
    # 统一成 1.8.0 这种规范形式：tag 可能是 v1.8.0-beta，而发布包名里是 1.8.0
    latest = ".".join(str(x) for x in parsed)

    # 资产里挑 zip。发布脚本固定命名 VPNTray-<ver>-win64.zip，但不写死，
    # 没有精确匹配就退回「唯一的 .zip」。
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    zip_name = f"VPNTray-{latest}-win64.zip"
    picked = next((a for a in assets if str(a.get("name")) == zip_name), None)
    if picked is None:
        zips = [a for a in assets if str(a.get("name", "")).lower().endswith(".zip")]
        picked = zips[0] if len(zips) == 1 else None

    size = 0
    dl = ""
    sha_asset = ""
    if picked is not None:
        try:
            size = int(picked.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        dl = str(picked.get("browser_download_url") or "")
        sha_asset = str(picked.get("digest") or "")

    return {
        "ok": True,
        "has_update": is_newer(latest, current),
        "current": current,
        "latest": latest,
        "tag": tag,
        "notes": str(data.get("body") or "")[:4000],
        "page": str(data.get("html_url") or ""),
        "zip_url": dl,
        "zip_size": size,
        "zip_sha256": sha_asset[7:] if sha_asset.startswith("sha256:") else "",
        "published": str(data.get("published_at") or ""),
    }


def download(url: str, dest: Path, local_port: int | None = None,
             on_progress: Callable[[int, int], None] | None = None,
             timeout: float = 20.0) -> Path:
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
            names = zf.namelist()
            if len(names) > 20000:
                raise UpdateError("安装包内文件数异常")
            for info in zf.infolist():
                raw = info.filename.replace("\\", "/")
                if raw.startswith("/") or ".." in raw.split("/"):
                    raise UpdateError(f"安装包内有可疑路径：{raw[:80]}")
                target = (payload / raw).resolve()
                if not str(target).startswith(str(payload.resolve())):
                    raise UpdateError("安装包路径越界")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, 256 * 1024)
    except zipfile.BadZipFile as e:
        raise UpdateError(f"安装包不是有效的 zip：{e}") from e

    root = find_exe_root(payload)
    if root is None:
        raise UpdateError("安装包里找不到 VPNTray.exe")

    # 版本自证：发布包里 assets/index.html 带着 UI_VER
    idx = root / "assets" / "index.html"
    if idx.exists():
        try:
            text = idx.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = re.search(r"const\s+UI_VER\s*=\s*['\"]([^'\"]+)['\"]", text)
        found = m.group(1) if m else ""
        if found and not found.startswith(expect_version.split(".")[0]):
            raise UpdateError(f"安装包版本对不上：期望 {expect_version}，包里是 {found}")
        if found and found != expect_version and not expect_version.startswith(found):
            log.warning("安装包 UI_VER=%s，Release 标的是 %s", found, expect_version)
    return root


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
setlocal
set "TARGET={target}"
set "PAYLOAD={payload}"
set "EXE={exe}"
rem 等上一份进程彻底退出，最多 40 秒
for /l %%i in (1,1,40) do (
    tasklist /fi "IMAGENAME eq %EXE%" 2>nul | find /i "%EXE%" >nul || goto gone
    timeout /t 1 /nobreak >nul
)
:gone
timeout /t 1 /nobreak >nul
rem /E 递归  /IS 覆盖同名  /IT 保留文件属性  /NP 不刷进度
robocopy "%PAYLOAD%" "%TARGET%" /E /IS /IT /R:3 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto failed
start "" "%TARGET%\\%EXE%"
rmdir /s /q "{workdir}" 2>nul
(goto) 2>nul & del "%~f0"
exit /b 0
:failed
echo 覆盖文件失败，旧版本仍在。按任意键关闭。
pause >nul
exit /b 1
"""


def stage(payload_root: Path, workdir: Path, target_root: Path | None = None,
          exe_name: str = "VPNTray.exe") -> Path:
    """写好 update.cmd 返回它。调用方随后应该让应用正常退出。"""
    target = Path(target_root or paths.ROOT).resolve()
    payload = Path(payload_root).resolve()
    workdir = Path(workdir).resolve()
    if not (payload / exe_name).exists():
        raise UpdateError("待安装目录里没有 VPNTray.exe")
    bat = workdir / "update.cmd"
    bat.write_bytes(
        BAT.format(target=str(target), payload=str(payload),
                   exe=str(exe_name), workdir=str(workdir))
        .replace("\n", "\r\n").encode("gbk", "replace")
    )
    return bat


def launch(bat: Path) -> None:
    """脱离父进程启动更新脚本，主进程死了它也继续跑。"""
    bat = Path(bat)
    if not bat.exists():
        raise UpdateError("更新脚本没写成功")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat)],
        cwd=str(bat.parent),
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
            on_progress: Callable[[int, int], None] | None = None) -> Path:
    """下载 → 校验 → 解压，返回 payload 根。不重启、不动当前文件。"""
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
    download(url, zip_path, local_port=local_port, on_progress=on_progress)

    want = str(info.get("zip_sha256") or "").lower()
    if want:
        got = sha256_of(zip_path)
        if got != want:
            zip_path.unlink(missing_ok=True)
            raise UpdateError("安装包校验和不匹配，已丢弃")
    else:
        log.warning("Release 没提供 sha256，跳过完整性校验")

    return extract(zip_path, work, latest)


__all__ = [
    "UpdateError", "parse_version", "is_newer", "check", "download", "sha256_of",
    "extract", "find_exe_root", "stage", "launch", "prepare", "update_workdir",
]
