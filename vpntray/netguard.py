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
import os
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

# TUN/TAP 网卡的驱动名片段。这里刻意不放 "tun" / "vpn" 这种泛词：
# 它们会命中 wintun0（我们自己拉起来的内核网卡）和「VPN 连接」这类系统自带拨号网卡，
# 于是「一键清理」把自家网卡列成冲突，用户一点就断网。
TUN_PATTERNS = ("wintun", "tap-", "tapwindows", "wintap", "wireguard",
                "sing-tun", "tun2socks", "clash", "mihomo")

# 虚拟网卡不是代理，只是名字里常带 tun/vpn，或者本身就是 TUN 的宿主。
# 单独列出来是为了「只报真实物理网卡」：禁掉 vEthernet 只会让用户的
# WSL / 虚拟机 / 容器断网，跟代理冲突毫无关系。
VIRTUAL_ADAPTER_PATTERNS = (
    "vethernet", "hyper-v", "wsl", "vmware", "virtualbox", "vbox",
    "tailscale", "zerotier", "docker", "npcap", "bluetooth", "蓝牙",
    "teredo", "isatap", "pseudo", "loopback",
)

# 从 GUI（pythonw / PyInstaller onedir）拉子进程时没有这个标志会闪黑框，
# 托盘程序闪框很容易被当成「中毒了」。0 在非 Windows 上对 subprocess 无害。
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$pat = 'clash|mihomo|verge|v2ray|xray|sing-box|singbox|neko|shadowsocks|ss-local|trojan|hiddify|flclash|qv2ray|wireguard|openvpn|warp|outline|psiphon|lantern|clash-meta'
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


def _no_window() -> dict[str, Any]:
    """子进程统一加「无窗口」标志；非 Windows 平台返回空字典走安全分支。"""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW}
    return {}


def _run_ps2(script: str, timeout: float = 20.0) -> tuple[dict[str, Any], str]:
    """跑一段 PowerShell，返回 (解析后的 JSON, 失败原因)。

    失败原因要单独交出去：调用方必须能区分「扫了，没冲突」和「根本没扫成」。
    以前一失败就返回 {}，界面显示成「没有冲突」，等于骗用户说环境已经干净了。
    """
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, **_no_window(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("调用 PowerShell 失败: %s", e)
        return {}, f"PowerShell 启动失败: {e}"
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if not out:
        if p.returncode:
            log.debug("PowerShell 退出码 %s: %s", p.returncode, err[:300])
            return {}, f"PowerShell 退出码 {p.returncode}: {err[:300]}"
        return {}, "PowerShell 没有输出"
    try:
        data = json.loads(out)
    except ValueError:
        log.debug("PowerShell 输出不是 JSON: %s", out[:200])
        return {}, f"PowerShell 输出不是 JSON: {out[:200]}"
    if not isinstance(data, dict):
        return {}, "PowerShell 输出结构不是对象"
    return data, ""


def _run_ps(script: str, timeout: float = 20.0) -> dict[str, Any]:
    """旧调用方只要数据：保持签名不变，内部走 _run_ps2。"""
    return _run_ps2(script, timeout)[0]


def _is_proxy_app(name: str) -> bool:
    """进程名算不算「别的」代理客户端。

    mihomo 内核有好几个发行名字：Clash.Meta 的 clash-meta.exe、Verge 的
    verge-mihomo.exe、还有些打包直接把内核改名成 meta.exe。只认 "mihomo"
    会漏掉它们，用户的旧内核就一直在后台和我们的抢路由表。
    """
    low = (name or "").lower()
    if any(pat in low for pat in PROXY_APP_PATTERNS):
        return True
    stem = low[:-4] if low.endswith(".exe") else low
    return stem in ("meta", "mihomo", "clash", "clash-meta", "verge-mihomo")


def _is_virtual_adapter(name: str, desc: str) -> bool:
    hay = f"{name} {desc}".lower()
    return any(pat in hay for pat in VIRTUAL_ADAPTER_PATTERNS)


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
    data, why = _run_ps2(_PS)

    # scan_ok=False 的意思是「没扫成」，不是「扫过、没冲突」。
    # 这两种情况给用户的结论正好相反，混在一起会让人以为机器已经干净了。
    scan_ok = not why

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
        if not _is_proxy_app(name):
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
        if _is_virtual_adapter(name, desc):
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
        "scan_ok": scan_ok,
        "scan_error": why,
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

    # 先把「接管前的原始值」冻结下来。清理只改当前状态，绝不改写备份 ——
    # 备份是退出时还原的唯一依据；以前这里往备份里写 {ProxyEnable:0, ...}，
    # 用户原本的代理设置就被永久覆盖，再想恢复也无从恢复。
    try:
        sysproxy.freeze_backup()  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        failed.append(f"冻结原始代理设置: {e}")
        log.warning("冻结系统代理备份失败: %s", e)

    step(lambda: sysproxy._write("ProxyEnable", 0, winreg.REG_DWORD),  # noqa: SLF001
         "关闭 ProxyEnable")
    for name in ("ProxyServer", "AutoConfigURL", "ProxyOverride"):
        step(lambda n=name: sysproxy._delete(n), f"删除 {name}")  # noqa: SLF001
    step(sysproxy._internet_set_option, "刷新 WinINet 设置")  # noqa: SLF001

    # 用户这次是主动要直连：单独记一个标记（不动备份），
    # 退出时就不会照着老备份把那个已经死掉的旧代理又装回去。
    try:
        sysproxy.mark_direct(True)  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        failed.append(f"记录直连意图: {e}")

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
                               capture_output=True, timeout=15, **_no_window())
            ok = p.returncode == 0
            done.append({"pid": pid, "ok": ok,
                         "msg": "" if ok else (p.stderr or b"").decode("utf-8", "replace")[:120]})
        except (OSError, subprocess.SubprocessError) as e:
            done.append({"pid": pid, "ok": False, "msg": str(e)[:120]})
    return {"ok": all(d["ok"] for d in done) if done else True, "results": done}


def disable_adapters(names: list[str]) -> dict[str, Any]:
    """禁用虚拟网卡（需要管理员，否则 PowerShell 会报 Access denied）。"""
    # 非管理员时 PowerShell 只会抛 Access denied，白跑一圈还可能弹 UAC。
    # 直接返回结构化 need_admin，界面才能提示「以管理员身份重开」而不是静默失败。
    if not is_admin():
        return {"ok": False, "need_admin": True, "results": [],
                "msg": "需要管理员权限才能禁用网卡"}
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
                capture_output=True, timeout=25, **_no_window(),
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


# ────────────────────── 门户认证（captive portal）探测 ──────────────────────
# 校园网 / 酒店 / 机场这种「先连上 WiFi 但还得打开网页认证」的网络，
# 在认证之前任何 HTTP 都会被网关劫持到认证页。
# 这种时候把系统代理指到还没起来的内核端口，等于把用户的浏览器彻底锁死：
# 认证页自己都得走代理，代理又不通 —— 用户连修都无从下手（火绒断网修复就是清代理）。
#
# 探针选的都是「必须返回固定内容」的公网地址，直连打（不走系统代理）。
CAPTIVE_PROBES = (
    ("http://connect.rom.miui.com/generate_204", 204, None),
    ("http://www.gstatic.com/generate_204", 204, None),
    ("http://www.msftconnecttest.com/connecttest.txt", 200, "Microsoft Connect Test"),
)

_PORTAL_CACHE: dict[str, Any] = {"ts": 0.0, "blocked": False, "reason": "", "how": ""}
_PORTAL_TTL = 25.0


def portal_status(timeout: float = 3.0, use_cache: bool = True) -> tuple[bool, str]:
    """判断当前网络是不是「还没通」（没过认证 / 干脆断网）。

    返回 (blocked, reason)：
      blocked=True 时 reason 是 `captive`（被门户劫持）或 `offline`（完全连不上）。

    探针都返回预期内容才算通。只要有一个通就算通 —— 不同运营商可能拦掉其中某几个。
    """
    import time as _t

    now = _t.time()
    if use_cache and now - float(_PORTAL_CACHE["ts"]) < _PORTAL_TTL:
        return bool(_PORTAL_CACHE["blocked"]), str(_PORTAL_CACHE["reason"])

    import requests

    saw_http = False   # 收到了 HTTP 响应（哪怕内容不对）→ 说明链路是通的，只是被劫持
    how = ""
    try:
        with requests.Session() as s:
            # 绝对直连：既不看环境变量，也不看注册表里的系统代理
            s.trust_env = False
            s.proxies = {"http": None, "https": None}
            for url, want_code, want_text in CAPTIVE_PROBES:
                try:
                    with s.get(url, timeout=timeout, allow_redirects=False,
                               headers={"User-Agent": "Mozilla/5.0 VPNTray-probe"}) as r:
                        saw_http = True
                        if r.status_code != want_code:
                            how = f"{url} -> HTTP {r.status_code}"
                            continue
                        if want_text is not None:
                            body = (r.text or "")[:200]
                            if want_text not in body:
                                how = f"{url} -> 内容不符（多半是门户页）"
                                continue
                        # 命中一个就算通
                        _PORTAL_CACHE.update({"ts": now, "blocked": False, "reason": "", "how": url})
                        return False, ""
                except Exception as e:  # noqa: BLE001
                    how = f"{url} -> {type(e).__name__}"
                    continue
    except Exception as e:  # noqa: BLE001
        how = f"探测异常 {type(e).__name__}"

    reason = "captive" if saw_http else "offline"
    _PORTAL_CACHE.update({"ts": now, "blocked": True, "reason": reason, "how": how})
    return True, reason


def portal_hint(reason: str) -> str:
    """给用户看的一句话，别只说「没网」。"""
    if reason == "captive":
        return "当前网络需要先登录（校园网 / 酒店那种），系统代理先不接管，登录完会自动接上"
    return "当前网络还没通，系统代理先不接管，网络恢复后会自动接上"


__all__ = ["scan", "wipe_system_proxy", "kill_apps", "disable_adapters",
           "system_proxy_state", "is_admin", "PROXY_APP_PATTERNS",
           "TUN_PATTERNS", "VIRTUAL_ADAPTER_PATTERNS",
           "portal_status", "portal_hint"]
