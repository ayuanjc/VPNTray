"""代理冲突清理：找出机器上「别的」代理软件，并让系统代理彻底回到直连。

为什么需要这个：用户机器上常常同时开着 Clash Verge / v2rayN / SingBox。
它们各自干三件事，凑在一起必然打架：

1. **抢 WinINET 系统代理**（注册表 ProxyEnable / ProxyServer）——
   后写的一方赢，于是「明明连上了却不通」；我们的健康看护线程又会每 20 秒
   往回抢一次，两边互相打架，表现为网络时通时断。
2. **PAC 自动配置**（AutoConfigURL）—— 这个优先级高于 ProxyServer，
   只要它还在，改 ProxyServer 等于没改。
3. **TUN 虚拟网卡**（wintun / tap / sing-tun）—— 它直接改路由表，
   系统代理关了也照样接管全部流量，表现是「关掉代理还是走代理」。

这里只做「检测 + 报告 + 用户点确认后动手」：
杀进程、禁网卡都会打断用户正在用的东西，还可能要求管理员权限，
所以绝不自动执行。
"""
from __future__ import annotations

import json
import logging
import subprocess
import winreg
from typing import Any

from . import sysproxy

log = logging.getLogger("netguard")

# 常见代理客户端的进程名片段（小写比较）。mihomo 也在里面，调用方要把它自己
# 那个内核的 PID 排除掉，否则「一键清理」会把自己的内核杀了。
PROXY_APP_PATTERNS = (
    "clash", "mihomo", "verge", "v2ray", "v2raya", "xray", "sing-box", "singbox",
    "nekobox", "nekoray", "nekoray", "shadowsocks", "ss-local", "ssr", "trojan",
    "hiddify", "flclash", "qv2ray", "wireguard", "openvpn", "warp-svc",
    "cloudflare-warp", "outline", "psiphon", "lantern",
)

# TUN/TAP 网卡的驱动名片段
TUN_PATTERNS = ("wintun", "tap-", "tapwindows", "tun", "wireguard", "sing-tun",
                "clash", "mihomo", "vpn", "wintap")

_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$pat = 'clash|mihomo|verge|v2ray|xray|sing-box|singbox|neko|shadowsocks|ss-local|trojan|hiddify|flclash|qv2ray|wireguard|openvpn|warp|outline|psiphon|lantern'
$procs = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match $pat } |
    Select-Object @{n='pid';e={[int]$_.ProcessId}},
                  @{n='name';e={$_.Name}},
                  @{n='path';e={$_.ExecutablePath}}
$adaps = Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } |
    Select-Object @{n='name';e={$_.Name}},
                  @{n='desc';e={$_.InterfaceDescription}},
                  @{n='idx';e={[int]$_.ifIndex}}
@{ procs = @($procs); adaps = @($adaps) } | ConvertTo-Json -Depth 4 -Compress
"""


def _run_ps(script: str, timeout: float = 20.0) -> dict[str, Any]:
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("调用 PowerShell 失败: %s", e)
        return {}
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        if p.returncode:
            log.debug("PowerShell 退出码 %s: %s", p.returncode,
                      (p.stderr or b"").decode("utf-8", "replace")[:300])
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        log.debug("PowerShell 输出不是 JSON: %s", out[:200])
        return {}
    return data if isinstance(data, dict) else {}


def system_proxy_state() -> dict[str, Any]:
    """当前 WinINET 代理状态，纯读，不改任何东西。"""
    return {
        "enabled": bool(sysproxy._read("ProxyEnable")),      # noqa: SLF001
        "server": sysproxy._read("ProxyServer") or "",       # noqa: SLF001
        "pac": sysproxy._read("AutoConfigURL") or "",        # noqa: SLF001
        "override": sysproxy._read("ProxyOverride") or "",   # noqa: SLF001
    }


def scan(exclude_pids: set[int] | None = None) -> dict[str, Any]:
    """扫一遍：系统代理 / PAC / 第三方代理进程 / TUN 网卡。

    exclude_pids 里放我们自己的进程（app 和它拉起来的 mihomo），否则会被
    当成「别人的代理」列出来，用户一点清理就把自己杀了。
    """
    exclude = set(exclude_pids or ())
    data = _run_ps(_PS)

    procs: list[dict[str, Any]] = []
    for item in data.get("procs") or []:
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or pid in exclude:
            continue
        name = str(item.get("name") or "")
        if not any(pat in name.lower() for pat in PROXY_APP_PATTERNS):
            continue
        procs.append({"pid": pid, "name": name,
                      "path": str(item.get("path") or "")})

    adapters: list[dict[str, Any]] = []
    for item in data.get("adaps") or []:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("desc") or "")
        name = str(item.get("name") or "")
        hay = f"{name} {desc}".lower()
        if not any(pat in hay for pat in TUN_PATTERNS):
            continue
        try:
            idx = int(item.get("idx") or 0)
        except (TypeError, ValueError):
            idx = 0
        adapters.append({"name": name, "desc": desc, "index": idx})

    st = system_proxy_state()
    return {
        "system_proxy": st,
        "apps": procs,
        "adapters": adapters,
        "conflict": bool(st["enabled"] or st["pac"] or procs or adapters),
    }


def wipe_system_proxy() -> dict[str, Any]:
    """把系统代理彻底关成直连：ProxyEnable=0，并清掉 ProxyServer / PAC。

    这里不走 sysproxy.disable()：那个是「还原到接管前的值」，而用户要的是
    「不管之前是什么，现在就是直连」。两步都做，顺序是先把开关关掉，
    再删地址 —— 反过来会有一瞬间「开关还开着但地址已经没了」，
    期间任何请求都会失败。
    """
    before = system_proxy_state()
    failed: list[str] = []

    def step(fn, what: str) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed.append(f"{what}: {e}")
            log.warning("清理系统代理 %s 失败: %s", what, e)

    step(lambda: sysproxy._write("ProxyEnable", 0, winreg.REG_DWORD),  # noqa: SLF001
         "关闭 ProxyEnable")
    for name in ("ProxyServer", "AutoConfigURL", "ProxyOverride"):
        step(lambda n=name: sysproxy._delete(n), f"删除 {name}")  # noqa: SLF001
    sysproxy._internet_set_option()  # noqa: SLF001

    # 同时把备份标记成「直连」：否则退出时 disable() 会照着老备份
    # 把用户的旧代理又装回去，白清理一场。
    try:
        sysproxy._save_backup({"ProxyEnable": 0, "ProxyServer": None,  # noqa: SLF001
                               "ProxyOverride": None, "AutoConfigURL": None})
    except Exception as e:  # noqa: BLE001
        failed.append(f"重置备份: {e}")

    after = system_proxy_state()
    log.info("系统代理已清理：%s -> %s", before.get("server") or "空", after)
    return {"before": before, "after": after, "failed": failed,
            "ok": not after["enabled"] and not after["pac"]}


def kill_apps(pids: list[int]) -> dict[str, Any]:
    """结束指定进程。调用方负责让用户确认过。"""
    done: list[dict[str, Any]] = []
    for pid in pids[:32]:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        try:
            p = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=15)
            ok = p.returncode == 0
            done.append({"pid": pid, "ok": ok,
                         "msg": "" if ok else (p.stderr or b"").decode("utf-8", "replace")[:120]})
        except (OSError, subprocess.SubprocessError) as e:
            done.append({"pid": pid, "ok": False, "msg": str(e)[:120]})
    return {"ok": all(d["ok"] for d in done) if done else True, "results": done}


def disable_adapters(names: list[str]) -> dict[str, Any]:
    """禁用虚拟网卡（需要管理员，否则 PowerShell 会报 Access denied）。"""
    done: list[dict[str, Any]] = []
    for name in names[:16]:
        name = str(name or "").strip()
        if not name:
            continue
        script = (f"$ErrorActionPreference='Stop';"
                  f"Disable-NetAdapter -Name '{name.replace(chr(39), chr(39) * 2)}' "
                  f"-Confirm:$false -ErrorAction Stop; 'ok'")
        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=25,
            )
            ok = p.returncode == 0 and b"ok" in (p.stdout or b"")
            done.append({"name": name, "ok": ok,
                         "msg": "" if ok else (p.stderr or b"").decode("utf-8", "replace")[:160]})
        except (OSError, subprocess.SubprocessError) as e:
            done.append({"name": name, "ok": False, "msg": str(e)[:160]})
    return {"ok": all(d["ok"] for d in done) if done else True, "results": done}


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


__all__ = ["scan", "wipe_system_proxy", "kill_apps", "disable_adapters",
           "system_proxy_state", "is_admin", "PROXY_APP_PATTERNS"]
