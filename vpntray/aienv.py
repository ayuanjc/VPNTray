"""AI 编程工具的环境变量与链路（1.3.0）。

为什么需要这一层：Claude Code / Codex / Gemini CLI 都是命令行程序，它们不读
Windows 的系统代理（WinINET 设置），只认 HTTP_PROXY / HTTPS_PROXY 这类环境
变量。模块分流只解决"内核把包送去哪"，管不到"程序有没有把包发给内核"：
tun 关着、程序又没设代理时，这些 CLI 的流量是直连出去的，走哪个节点都与它无关。

所以这里的职责只有两件：
  1) 认出来机器上有哪些 CLI（只查 PATH，不启动它们）；
  2) 帮用户把 HTTPS_PROXY 指到本机 mixed 端口，并且能一键还原原值。

边界：只写 HKCU 的 Environment 键（当前用户，不需要管理员），只写这一次改动涉及的三类键，
改动前把原值备份到 data/aienv-backup.json；不碰系统级环境变量，不碰 netsh，不写 PATH。
"""
from __future__ import annotations

import ctypes
import json
import pathlib
import shutil
import socket
import ssl
import time
import winreg
from typing import Any

from . import paths

ENV_KEY = r"Environment"
PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
NO_PROXY_KEY = "NO_PROXY"
BACKUP_NAME = "aienv-backup.json"

# 认得出、也值得认的四个 CLI：key, 显示名, 可执行名候选, 探测用主机
TOOLS = (
    ("claude", "Claude Code", ("claude.cmd", "claude.exe", "claude"), "api.anthropic.com"),
    ("codex", "Codex CLI", ("codex.cmd", "codex.exe", "codex"), "api.openai.com"),
    ("gemini", "Gemini CLI", ("gemini.cmd", "gemini.exe", "gemini"),
     "generativelanguage.googleapis.com"),
    ("antigravity", "Antigravity", ("antigravity.exe", "antigravity"), "antigravity.google"),
    # ccr = Claude Code Router：给 Claude Code 换上游的中转站，用户装了它就一定在用它
    ("ccr", "Claude Code Router", ("ccr.cmd", "ccr.exe", "ccr"), "api.anthropic.com"),
)

NO_PROXY_ADD = ("localhost", "127.0.0.1", "::1", "*.local", ".local")
# 探测超时：CONNECT 与 TLS 分开。mihomo 在 global 模式下要出国绕一圈，
# 5 秒给不够 —— 实测 generativelanguage.googleapis.com 在 5 秒时刚好卡在握手。
CONNECT_TIMEOUT = 8.0
TLS_TIMEOUT = 12.0
PROBE_TIMEOUT = CONNECT_TIMEOUT


def _backup_path() -> pathlib.Path:
    return pathlib.Path(paths.DATA) / BACKUP_NAME


def _read_env() -> dict[str, str]:
    """读 HKCU 的 Environment 键 里我们关心的键（读不到就是没设）。"""
    out: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENV_KEY) as k:
            for name in PROXY_KEYS + (NO_PROXY_KEY,):
                try:
                    v, _ = winreg.QueryValueEx(k, name)
                    out[name] = str(v)
                except FileNotFoundError:
                    continue
    except OSError:
        pass
    return out


def _write_env(values: dict[str, str]) -> str:
    """写 HKCU 的 Environment 键，并广播 WM_SETTINGCHANGE。返回空串表示成功。

    注意：广播只让新开的进程看到新值，已经在跑的终端要重开 —— 这条写进界面提示，
    不然用户会以为"点了按钮没生效"。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, ENV_KEY, 0, winreg.KEY_SET_VALUE) as k:
            for name, val in values.items():
                if val == "":
                    try:
                        winreg.DeleteValue(k, name)
                    except FileNotFoundError:
                        pass
                else:
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
    except OSError as e:
        return "写环境变量失败：%s" % e
    try:
        # HWND_BROADCAST=0xFFFF, WM_SETTINGCHANGE=0x1A, SMTO_ABORTIFHUNG=2
        ctypes.windll.user32.SendMessageTimeoutW(
            ctypes.c_void_p(0xFFFF), 0x1A, 0, ctypes.c_wchar_p("Environment"), 2, 3000, None)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _proxy_url(port: int) -> str:
    return "http://127.0.0.1:%d" % int(port)


def _merge_no_proxy(old: str) -> str:
    parts = [x.strip() for x in (old or "").split(",") if x.strip()]
    low = {x.lower() for x in parts}
    for want in NO_PROXY_ADD:
        if want.lower() not in low:
            parts.append(want)
            low.add(want.lower())
    return ",".join(parts)


def _tls_ok(sock: socket.socket, host: str, timeout: float = TLS_TIMEOUT) -> tuple[bool, str]:
    try:
        sock.settimeout(timeout)
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=host) as ss:
            return True, ""
    except Exception as e:  # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


def _via_proxy(host: str, port: int, proxy_port: int, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    """用 CONNECT 隧道验证"经本机 mixed 端口能否与目标完成 TLS 握手"。"""
    t0 = time.time()
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", int(proxy_port)), timeout=timeout)
        sock.settimeout(timeout)
        req = "CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n" % (host, host)
        sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < 4096:
            chunk = sock.recv(512)
            if not chunk:
                break
            buf += chunk
        head = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 200" not in head:
            return {"ok": False, "ms": int((time.time() - t0) * 1000),
                    "error": "代理拒绝 CONNECT：%s" % head[:80]}
        ok, err = _tls_ok(sock, host)
        ms = int((time.time() - t0) * 1000)
        return {"ok": ok, "ms": ms, "error": err}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _direct(host: str, timeout: float = PROBE_TIMEOUT) -> dict[str, Any]:
    t0 = time.time()
    sock = None
    try:
        sock = socket.create_connection((host, 443), timeout=timeout)
        ok, err = _tls_ok(sock, host)
        return {"ok": ok, "ms": int((time.time() - t0) * 1000), "error": err}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _which(names: tuple[str, ...]) -> str:
    for name in names:
        p = shutil.which(name)
        if p:
            return p
    return ""


def status(mixed_port: int = 7891, probe: bool = False) -> dict[str, Any]:
    """工具清单 + 当前环境变量 + 是否由我们设过（有备份）。probe=True 才联网验证。"""
    env = _read_env()
    tools = []
    for key, title, names, host in TOOLS:
        path = _which(names)
        item: dict[str, Any] = {
            "key": key,
            "title": title,
            "found": bool(path),
            "path": path,
            "host": host,
        }
        if path and probe:
            item["via_proxy"] = _via_proxy(host, 443, mixed_port)
            item["direct"] = _direct(host)
        tools.append(item)
    want = _proxy_url(mixed_port)
    cur = env.get("HTTPS_PROXY") or env.get("https_proxy") or ""
    return {
        "ok": True,
        "tools": tools,
        "env": env,
        "want": want,
        "applied": cur.strip().lower() == want,
        "mixed_port": int(mixed_port),
        "has_backup": _backup_path().exists(),
        "note": "环境变量只对新开的终端生效；已在跑的 Claude Code / Codex 要重启。",
    }


def apply(mixed_port: int = 7891) -> dict[str, Any]:
    """把代理指到本机 mixed 端口。第一次调用会把原值备份下来。"""
    port = int(mixed_port)
    url = _proxy_url(port)
    old = _read_env()
    bak = _backup_path()
    if not bak.exists():
        try:
            bak.parent.mkdir(parents=True, exist_ok=True)
            bak.write_text(json.dumps({"env": old, "port": port}, ensure_ascii=False, indent=1),
                           encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": "备份原环境变量失败，为安全起见不做改动：%s" % e}
    values = {
        "HTTPS_PROXY": url,
        "HTTP_PROXY": url,
        "https_proxy": url,
        "http_proxy": url,
        NO_PROXY_KEY: _merge_no_proxy(old.get(NO_PROXY_KEY, "")),
    }
    err = _write_env(values)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "applied": True, "env": values,
            "note": "已指向 %s。新开终端生效；已开的窗口要重开。" % url}


def clear() -> dict[str, Any]:
    """还原到备份里的原值（没有备份就只删我们自己写的那几个键）。"""
    bak = _backup_path()
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"ok": False, "error": "备份读取失败，不敢乱删：%s" % e}
        old = data.get("env") if isinstance(data, dict) else None
        if not isinstance(old, dict):
            return {"ok": False, "error": "备份内容不认识，未做改动"}
        values = {name: str(old.get(name) or "") for name in PROXY_KEYS + (NO_PROXY_KEY,)}
        err = _write_env(values)
        if err:
            return {"ok": False, "error": err}
        try:
            bak.unlink()
        except OSError:
            pass
        return {"ok": True, "applied": False, "restored": values,
                "note": "已还原成原来的环境变量（没有的键已删除）。"}
    # 没有备份：只删值形如本机代理的键，别动用户自己的设置
    cur = _read_env()
    values = {}
    for name in PROXY_KEYS:
        v = (cur.get(name) or "").strip().lower()
        if v.startswith("http://127.0.0.1:") or v.startswith("http://localhost:"):
            values[name] = ""
    if not values:
        return {"ok": True, "applied": False, "note": "没有本软件设过的代理变量，未做改动。"}
    err = _write_env(values)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "applied": False, "note": "已删掉指向本机代理的环境变量。"}

