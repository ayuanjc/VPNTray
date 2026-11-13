# -*- coding: utf-8 -*-
"""断网急救：把「没网」拆成能逐条判定的检查项，每项自带修复动作。

设计上的取舍（别改反）：

1. **只读优先**。probe() 绝不改任何东西，改系统的动作全在 fix() 里，
   每个动作单独可回滚（DNS 改回自动、hosts 带备份）。
2. **每项自带修法**。报告里每一条 bad/warn 都带 fix 字段，UI 就地出「修」按钮，
   不用让用户猜「那我现在该点哪个」。
3. **需要管理员的动作不自动跑**。禁网卡、winsock reset、改 DNS 都要提权，
   一键修复只跑安全集合，剩下的大声标 need_admin，让用户自己按。
4. **专业详情不藏**。每项都带 detail（原始命令 + 原始输出），默认折叠，
   点开就是能直接贴给别人的证据。
5. **探测必须快**。整轮体检控制在 ~4 秒：ping 只打 3 包、DNS 用 UDP 直问、
   门户探针走 netguard 的 25 秒缓存。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import struct
import subprocess
import time
from typing import Any

from . import dnsguard, hotspot, netguard

log = logging.getLogger("netdoctor")

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 系统 DNS 被指到本机但本机解析没起来 = 全机断网（Node / Go / 部分游戏客户端首当其冲）。
LOOPBACK_DNS = ("127.0.0.1", "::1", "127.0.0.2")

# 公网 DNS 兜底顺序（国内优先，119 是腾讯，223 阿里，114 是老牌）
DNS_FALLBACK = ("223.5.5.5", "119.29.29.29", "114.114.114.114")

# 体检要问的域名：一个国内一个国外，判断「只有国外不通」还是「全都不通」
PROBE_NAMES = (("www.baidu.com", "国内"), ("dashscope.aliyuncs.com", "国内 API"),
               ("www.gstatic.com", "国外"))


# ────────────────────────── 跑命令 ──────────────────────────

def _run(argv: list[str], timeout: float = 6.0) -> dict[str, Any]:
    """跑一条命令，返回 {ok, code, out, err}。解码按 GBK→UTF-8 顺序试。"""
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "code": -1, "out": "", "err": f"{type(e).__name__}: {e}"}
    raw = p.stdout or b""
    text = ""
    for enc in ("gbk", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw.decode("gbk", "replace")
    return {"ok": p.returncode == 0, "code": int(p.returncode),
            "out": text, "err": (p.stderr or b"").decode("gbk", "replace")[:400]}


def _ping(host: str, count: int = 3) -> dict[str, Any]:
    """ping 一次，拿丢包率和平均延迟。中英文输出都认。"""
    r = _run(["ping", "-n", str(count), "-w", "1200", host], timeout=count * 1.6 + 3)
    out = r["out"]
    loss = None
    m = re.search(r"(\d+)\s*%\s*(?:loss|丢失)", out, re.I)
    if m:
        loss = int(m.group(1))
    times = [int(x) for x in re.findall(r"(?:time|时间)\s*[=<]\s*(\d+)\s*ms", out, re.I)]
    avg = None
    m = re.search(r"(?:Average|平均)\s*=\s*(\d+)\s*ms", out, re.I)
    if m:
        avg = int(m.group(1))
    elif times:
        avg = sum(times) // len(times)
    return {"ok": bool(times) and loss in (0, None), "loss": loss, "avg": avg,
            "times": times[:6], "cmd": f"ping -n {count} -w 1200 {host}", "raw": out[:1200]}


_NETSH_IF = re.compile(r'^(?:Configuration for interface|接口)\s*["\u201c]?([^"\u201d]+?)["\u201d]?\s*(?:的配置)?$', re.I)


def _dns_servers() -> dict[str, Any]:
    """读网卡上配的 DNS。

    走 Get-DnsClientServerAddress 而不是 netsh：netsh 的排版逐版本变（这行在
    中文系统里叫「通过 DHCP 配置的 DNS 服务器」，在英文系统里叫别的），
    PowerShell 给的是结构化的 InterfaceAlias + ServerAddresses，机器好读。
    static 标志仍然靠 netsh（只有它明说「静态配置」），读不到就当未知。
    """
    # 只用 [pscustomobject] + 逗号拼串：别名表 @{n=..;e=..} 在 -Command 里
    # 常被序列化成 {value:[..],Count:n}，解析出来是字符串 "value"/"Count"。
    ps = ("Get-DnsClientServerAddress -AddressFamily IPv4 | "
          "ForEach-Object { [pscustomobject]@{ iface = $_.InterfaceAlias; "
          "dns = (@($_.ServerAddresses) -join ',') } } | "
          "ConvertTo-Json -Depth 3 -Compress")
    r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=12)
    per_if: list[dict[str, Any]] = []
    servers: list[str] = []
    try:
        import json as _json
        data = _json.loads(r["out"]) if r["out"].strip() else []
        if isinstance(data, dict):
            data = [data]
        for item in data or []:
            if not isinstance(item, dict):
                continue
            addrs = [x.strip() for x in str(item.get("dns") or "").split(",") if x.strip()]
            addrs = [str(a) for a in addrs if a]
            if not addrs:
                continue
            per_if.append({"iface": str(item.get("iface") or item.get("InterfaceAlias") or ""),
                           "servers": addrs, "static": False})
            for a in addrs:
                if a not in servers:
                    servers.append(a)
    except Exception as e:  # noqa: BLE001
        log.debug("解析 Get-DnsClientServerAddress 失败: %s", e)

    st = _run(["netsh", "interface", "ipv4", "show", "dnsservers"], timeout=8)
    out = st["out"] or ""
    if per_if:
        # 静态标志：netsh 的输出里找「静态配置 / statically configured」
        if re.search(r"静态|statically", out, re.I):
            for item in per_if:
                item["static"] = True
    else:
        # PowerShell 挂了就退回 netsh 解析。表头是 接口 "WLAN" 的配置，
        # 「DNS 服务器」在下一行，所以表头正则不能要求同一行还带那几个字。
        for line in out.splitlines():
            s = line.strip()
            m = _NETSH_IF.match(s)
            if m:
                per_if.append({"iface": m.group(1).strip(), "servers": [], "static": False})
                continue
            if not per_if:
                continue
            if re.search(r"statically configured|静态配置", s, re.I):
                per_if[-1]["static"] = True
            for ip in re.findall(r"(?:\d{1,3}\.){3}\d{1,3}", s):
                if ip not in per_if[-1]["servers"]:
                    per_if[-1]["servers"].append(ip)
        for item in per_if:
            for ip in item["servers"]:
                if ip not in servers:
                    servers.append(ip)
    return {"servers": servers, "per_if": per_if,
            "static": any(i["static"] for i in per_if),
            "cmd": "netsh interface ipv4 show dnsservers", "raw": out[:1600]}


def _build_query(name: str, qtype: int = 1) -> bytes:
    tid = os.urandom(2)
    header = tid + struct.pack("!HHHHH", 0x0100, 1, 0, 0, 0)
    q = b"".join(bytes([len(p)]) + p.encode("idna") for p in name.rstrip(".").split("."))
    return header + q + b"\x00" + struct.pack("!HH", qtype, 1)


def _ask(server: str, name: str, timeout: float = 1.6) -> dict[str, Any]:
    """直接向某个 DNS 服务器问一次 A 记录，返回耗时与答案个数。"""
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(_build_query(name), (server, 53))
        data, _ = s.recvfrom(2048)
    except OSError as e:
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "error": type(e).__name__, "answers": 0}
    finally:
        s.close()
    ms = int((time.time() - t0) * 1000)
    if len(data) < 12:
        return {"ok": False, "ms": ms, "error": "short reply", "answers": 0}
    ancount = struct.unpack("!H", data[6:8])[0]
    rcode = data[3] & 0x0F
    return {"ok": rcode == 0 and ancount > 0, "ms": ms, "answers": int(ancount),
            "rcode": rcode, "error": "" if rcode == 0 else f"rcode={rcode}"}


def _listen(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        s.close()


def _global_ipv6() -> list[str]:
    r = _run(["netsh", "interface", "ipv6", "show", "address"], timeout=8)
    out = r["out"]
    found: list[str] = []
    for ip in re.findall(r"\b([0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){2,7})\b", out):
        try:
            addr = ipaddress.IPv6Address(ip)
        except ValueError:
            continue
        if addr.is_link_local or addr.is_loopback or addr.is_multicast:
            continue
        if str(addr) not in found:
            found.append(str(addr))
    return found


def _mtu() -> dict[str, Any]:
    """用不分片 ping 找链路 MTU：1480 通了就是 1480+28=1508，1500 通是常规以太网。"""
    for size in (1472, 1400, 1300):
        r = _run(["ping", "-n", "1", "-f", "-l", str(size), "-w", "1500", "223.5.5.5"], timeout=4)
        out = r["out"]
        if re.search(r"(?:time|时间)\s*[=<]\s*\d+\s*ms", out, re.I):
            return {"ok": True, "mtu": size + 28, "payload": size,
                    "cmd": f"ping -f -l {size} 223.5.5.5", "raw": out[:600]}
    return {"ok": False, "mtu": 0, "payload": 0,
            "cmd": "ping -f -l 1472/1400/1300 223.5.5.5", "raw": "全部分片被丢弃（链路 MTU 小于 1328，或 ICMP 被拦）"}


# hosts 里出现这些 IP 只可能是「本地回环」或「屏蔽」，两种用途要分开看：
# 127.0.0.1 localhost 是标准行，绝不能当成屏蔽；0.0.0.0 example.com 才是屏蔽。
LOCAL_NAMES = frozenset({
    "localhost", "localhost.localdomain", "local", "broadcasthost",
    "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
    "ip6-allnodes", "ip6-allrouters", "ip6-allrouters.localdomain",
})
BLOCK_IPS = frozenset({"0.0.0.0", "127.0.0.1", "::1", "::", "0:0:0:0:0:0:0:1"})
STD_HOSTS_LINES = ("127.0.0.1  localhost", "::1  localhost")


def _hosts_path() -> str:
    """hosts 的真实路径；VPNTRAY_HOSTS_PATH 可以指到别处（测试 / 便携用法）。"""
    p = (os.environ.get("VPNTRAY_HOSTS_PATH") or "").strip()
    return p or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "drivers", "etc", "hosts")


def _hosts() -> dict[str, Any]:
    path = _hosts_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        size = os.path.getsize(path)
        writable = os.access(path, os.W_OK)
    except OSError as e:
        return {"ok": False, "path": path, "error": f"{type(e).__name__}: {e}",
                "blocked": [], "entries": 0, "dup": 0, "writable": False}
    lines = raw.splitlines()
    blocked: list[str] = []
    customs: list[str] = []
    entries = 0
    dup = 0
    seen: set[str] = set()
    for ln in lines:
        s = ln.split("#", 1)[0].strip()
        if not s:
            continue
        parts = s.split()
        if len(parts) < 2:
            continue
        entries += 1
        ip, names = parts[0], parts[1:]
        for nm in names:
            key = nm.lower()
            if key in seen:
                dup += 1
            seen.add(key)
        if ip in BLOCK_IPS:
            bad = [n for n in names if n.lower() not in LOCAL_NAMES]
            blocked.extend(bad[:6])
        else:
            customs.append(ip + "  " + " ".join(names))
    return {"ok": True, "path": path, "entries": entries, "dup": dup,
            "blocked": blocked[:40], "blocked_n": len(blocked),
            "customs": customs[:40], "writable": writable, "size": size,
            "raw": raw[:1500]}


# ────────────────────────── 单项检查 ──────────────────────────

def _ck(cid: str, title: str, level: str, brief: str, detail: str = "",
        fix: str = "", fix_title: str = "", need_admin: bool = False,
        data: Any = None) -> dict[str, Any]:
    return {"id": cid, "title": title, "level": level, "brief": brief,
            "detail": detail.strip(), "fix": fix, "fix_title": fix_title,
            "need_admin": bool(need_admin), "data": data}


def _kind_cn(ip: str) -> str:
    """按地址段猜链路类型。私有段里 172.16/12 常常是校园网 / 企业网。"""
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return "未知链路"
    if a.is_loopback:
        return "本机回环"
    if a in ipaddress.IPv4Network("192.168.0.0/16"):
        return "家用路由器 / 热点"
    if a in ipaddress.IPv4Network("10.0.0.0/8"):
        return "内网（10/8）"
    if a in ipaddress.IPv4Network("172.16.0.0/12"):
        return "内网（172.16/12，多为校园网 / 企业网）"
    if a in ipaddress.IPv4Network("100.64.0.0/10"):
        return "运营商级 NAT"
    if a.is_private:
        return "内网"
    return "非常见私网段（校园网 / 企业网常见，也可能是公网直连）"


def _chk_link(_app: Any) -> dict[str, Any]:
    info = hotspot.identity(refresh=True)
    ip, gw = str(info.get("ip") or ""), str(info.get("gw") or "")
    kind = str(info.get("kind") or "unknown")
    kind_cn = {"lan": "有线/局域网", "iphone_hotspot": "iPhone 热点",
               "android_hotspot": "安卓热点"}.get(kind, "") or _kind_cn(ip)
    detail = f"本机地址: {ip or '（无）'}\n默认网关: {gw or '（无）'}\n链路类型: {kind_cn}"
    if not ip:
        return _ck("link", "网卡 / 链路", "bad", "没有拿到本机地址",
                   detail + "\n\n拿不到地址通常是：网卡被禁用、Wi-Fi 没连上、DHCP 没发地址。",
                   fix="renew_ip", fix_title="重新获取地址")
    if not gw:
        return _ck("link", "网卡 / 链路", "warn", f"{ip} 但没读到默认网关",
                   detail + "\n\n没有默认网关就走不出本机——检查是不是被 TUN 网卡顶掉了默认路由。",
                   fix="renew_ip", fix_title="重新获取地址")
    return _ck("link", "网卡 / 链路", "ok", f"{ip} → 网关 {gw}（{kind_cn}）", detail, data=info)


def _chk_gateway(_app: Any) -> dict[str, Any]:
    gw = str(hotspot.identity().get("gw") or "")
    if not gw:
        return _ck("gateway", "网关连通", "skip", "没有网关可测")
    p = _ping(gw, 3)
    detail = f"{p['cmd']}\n丢包: {p['loss']}%  平均: {p['avg']}ms\n\n{p['raw']}"
    if p["loss"] == 100 or not p["times"]:
        return _ck("gateway", "网关连通", "bad", f"网关 {gw} 完全不通",
                   detail + "\n\n网关都不通，问题在本机到路由器这一段：Wi-Fi 信号、网线、路由器本身。",
                   fix="restart_adapter", fix_title="重启网卡", need_admin=True, data=p)
    if (p["loss"] or 0) > 0:
        return _ck("gateway", "网关连通", "warn", f"网关 {gw} 丢包 {p['loss']}%（{p['avg']}ms）",
                   detail + "\n\n偶发丢包多半是信号弱或 2.4G 干扰，频段换 5GHz 通常立刻见效。",
                   fix="restart_adapter", fix_title="重启网卡", need_admin=True, data=p)
    return _ck("gateway", "网关连通", "ok", f"网关 {gw} {p['avg']}ms 无丢包", detail, data=p)


def _chk_dns_cfg(_app: Any) -> dict[str, Any]:
    d = _dns_servers()
    servers = d["servers"]
    detail = (f"{d['cmd']}\n配置: {', '.join(servers) if servers else '（空，用 DHCP 下发的）'}"
              f"\n静态: {'是' if d['static'] else '否'}\n\n{d['raw']}")
    loop = [s for s in servers if s in LOOPBACK_DNS]
    if not servers:
        return _ck("dns_cfg", "系统 DNS 配置", "info", "没显式配 DNS，用 DHCP 下发的",
                   detail + "\n\nDHCP 下发的 DNS 常被运营商投毒或超时；网络时好时坏时固定成公共 DNS 更稳。",
                   fix="fix_dns", fix_title="固定公共 DNS", need_admin=True, data=d)
    if loop:
        others = [s for s in servers if s not in LOOPBACK_DNS]
        if others:
            return _ck("dns_cfg", "系统 DNS 配置", "ok",
                       f"本机 {loop[0]} + 兜底 {others[0]}", detail, data=d)
        return _ck("dns_cfg", "系统 DNS 配置", "warn", f"只指向本机 {loop[0]}，没有兜底",
                   detail + "\n\n只有本机解析一条路：它一挂，全机 DNS 就全挂。留一个公共 DNS 兜底。",
                   fix="fix_dns_keep_local", fix_title="补一个兜底 DNS", need_admin=True, data=d)
    if any(s.startswith(("10.", "192.168.", "172.")) for s in servers):
        return _ck("dns_cfg", "系统 DNS 配置", "warn", f"内网 DNS {servers[0]}（可能只解析内网域名）",
                   detail + "\n\n内网 DNS 常常解不出公网域名，表现是「内网能开外网全挂」。",
                   fix="fix_dns_keep_local", fix_title="补一个公共 DNS 兜底", need_admin=True, data=d)
    return _ck("dns_cfg", "系统 DNS 配置", "ok", f"{', '.join(servers[:3])}", detail, data=d)


def _chk_dns_local(app: Any) -> dict[str, Any]:
    """本机解析：系统 DNS 指着 127.0.0.1 时，这里必须有东西在听。"""
    d = _dns_servers()
    points_local = any(s in LOOPBACK_DNS for s in d["servers"])
    busy = dnsguard.port_busy(53)
    detail = (f"系统 DNS 是否指向本机: {'是' if points_local else '否'}"
              f"\n127.0.0.1:53 监听: {'是' if busy else '否'}")
    if busy:
        pr = dnsguard.probe() if hasattr(dnsguard, "probe") else {}
        ms = pr.get("ms") if isinstance(pr, dict) else None
        return _ck("dns_local", "本机解析 127.0.0.1:53", "ok",
                   f"在服务{('（' + str(ms) + 'ms）') if ms else ''}", detail, data={"busy": True})
    if points_local:
        return _ck("dns_local", "本机解析 127.0.0.1:53", "bad",
                   "系统 DNS 指向本机，但 53 没人监听 —— 全机断网",
                   detail + "\n\n这是最典型的「自己把自己搞断网」：DNS 指 127.0.0.1，"
                            "而本地解析器没起（或端口被别的程序占了）。\n"
                            "两种修法：把本地解析拉起来，或把系统 DNS 改回公共 DNS。",
                   fix="start_dns", fix_title="拉起本地解析")
    return _ck("dns_local", "本机解析 127.0.0.1:53", "info", "未启用", detail,
               fix="start_dns", fix_title="启用本地解析")


def _chk_dns_public(_app: Any) -> dict[str, Any]:
    """分别问系统 DNS 与公共 DNS，区分「DNS 挂了」和「链路挂了」。"""
    rows: list[str] = []
    bad_sys = 0
    for name, tag in PROBE_NAMES:
        t0 = time.time()
        try:
            socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
            ms = int((time.time() - t0) * 1000)
            rows.append(f"{tag} {name}: {ms}ms")
        except OSError as e:
            bad_sys += 1
            rows.append(f"{tag} {name}: 失败 {type(e).__name__}")
    pub: list[str] = []
    pub_ok = 0
    for srv in DNS_FALLBACK[:2]:
        r = _ask(srv, "www.baidu.com")
        pub.append(f"{srv}: {r.get('ms')}ms {r.get('answers')} 条" if r["ok"] else
                   f"{srv}: 失败 {r.get('error') or '无应答'}")
        pub_ok += 1 if r["ok"] else 0
    detail = "系统 DNS 解析：\n  " + "\n  ".join(rows) + "\n\n公共 DNS 直问：\n  " + "\n  ".join(pub)
    if bad_sys == 0:
        return _ck("dns_public", "域名解析", "ok", rows[0].split(": ")[-1] + " 可解析", detail)
    if bad_sys < len(PROBE_NAMES):
        return _ck("dns_public", "域名解析", "warn",
                   f"{bad_sys}/{len(PROBE_NAMES)} 个域名解析失败",
                   detail + "\n\n部分失败常见于：分流规则把域名送进了坏节点，或 DNS 只对国内域名有效。",
                   fix="fix_dns", fix_title="换公共 DNS", need_admin=True)
    if pub_ok:
        return _ck("dns_public", "域名解析", "bad",
                   "系统 DNS 全挂，公共 DNS 能通 —— 换 DNS 就能救",
                   detail + "\n\n判定：链路通、DNS 坏。直接换公共 DNS 即可恢复。",
                   fix="fix_dns", fix_title="换公共 DNS", need_admin=True)
    return _ck("dns_public", "域名解析", "bad", "系统 DNS 与公共 DNS 都不通",
               detail + "\n\n两边都不通，多半不是 DNS 的问题，而是链路/防火墙（看网关那项）。",
               fix="flush_dns", fix_title="清空 DNS 缓存")


def _chk_sysproxy(app: Any) -> dict[str, Any]:
    st = netguard.system_proxy_state()
    srv = str(st.get("server") or "")
    pac = str(st.get("pac") or "")
    parts = [f"ProxyEnable: {1 if st.get('enabled') else 0}", f"ProxyServer: {srv or '（空）'}",
             f"AutoConfigURL: {pac or '（空）'}"]
    if not st.get("enabled") and not pac:
        return _ck("sysproxy", "系统代理", "ok", "未接管（直连）", "\n".join(parts), data=st)
    host, port = "", 0
    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3}|localhost):(\d{2,5})", srv)
    if m:
        host, port = m.group(1), int(m.group(2))
    alive = _listen(port) if port else True
    detail = "\n".join(parts) + f"\n代理端口 {host}:{port or '?'} 监听: {'是' if alive else '否'}"
    if pac:
        return _ck("sysproxy", "系统代理", "warn", f"还挂着 PAC {pac[:40]}",
                   detail + "\n\nPAC 优先级高于 ProxyServer，改 ProxyServer 等于没改——先清 PAC。",
                   fix="fix_proxy", fix_title="清空系统代理", data=st)
    if port and not alive:
        return _ck("sysproxy", "系统代理", "bad",
                   f"系统代理指向 {host}:{port}，但那里没人监听 —— 浏览器全断",
                   detail + "\n\n这是断网最常见的死法：代理软件退了，注册表还指着它。"
                            "一键清空系统代理立刻恢复直连。",
                   fix="fix_proxy", fix_title="清空系统代理", data=st)
    return _ck("sysproxy", "系统代理", "ok", f"已指向 {host}:{port}", detail, data=st)


def _chk_kernel(app: Any) -> dict[str, Any]:
    ports = []
    for name, port in (("混合口", 7891), ("控制口", 9091)):
        ports.append((name, int(port), _listen(int(port))))
    alive = [p for p in ports if p[2]]
    detail = "\n".join(f"{n} {p}: {'LISTEN' if ok else '未监听'}" for n, p, ok in ports)
    if alive:
        return _ck("kernel", "代理内核", "ok", f"{len(alive)}/{len(ports)} 个端口在听", detail,
                   data={"ports": [{"name": n, "port": p, "ok": ok} for n, p, ok in ports]})
    return _ck("kernel", "代理内核", "bad", "7891 / 9091 都没监听（内核没起来）",
               detail + "\n\n内核没起来时，系统代理指过来就是断网。要么重启内核，要么关掉系统代理。",
               fix="restart_kernel", fix_title="重启内核", data={"ports": []})


def _chk_direct(_app: Any) -> dict[str, Any]:
    """直连出口：绕过系统代理打 204 探针。"""
    blocked, reason = netguard.portal_status(timeout=2.5, use_cache=True)
    hint = netguard.portal_hint(reason) if blocked else ""
    detail = f"portal_status -> blocked={blocked} reason={reason or '（通）'}"
    if not blocked:
        return _ck("direct", "直连出口", "ok", "204 探针通过（链路真的通网）", detail)
    if reason == "captive":
        return _ck("direct", "直连出口", "warn", "被门户劫持（需要先登录）",
                   detail + "\n\n" + hint, fix="open_portal", fix_title="打开认证页")
    return _ck("direct", "直连出口", "bad", "直连不通（204 探针失败）",
               detail + "\n\n" + hint + "\n看网关那项：网关通而这里不通，问题多半在账号欠费 / 上游运营商。")


def _chk_ipv6(_app: Any) -> dict[str, Any]:
    addrs = _global_ipv6()
    if addrs:
        return _ck("ipv6", "IPv6", "ok", f"有全局地址 {addrs[0]}",
                   "全局地址:\n  " + "\n  ".join(addrs[:4]))
    return _ck("ipv6", "IPv6", "info", "没有全局 IPv6 地址（不影响 IPv4 上网）",
               "netsh interface ipv6 show address 里没有非链路本地地址。")


def _chk_mtu(_app: Any) -> dict[str, Any]:
    m = _mtu()
    if m["ok"]:
        lvl = "ok" if m["mtu"] >= 1400 else "warn"
        brief = f"链路 MTU {m['mtu']}"
        extra = "" if m["mtu"] >= 1400 else "\n\nMTU 偏小会导致大包被丢：网页能开、视频/下载卡住。路由器上把 MTU 调回 1500。"
        return _ck("mtu", "链路 MTU", lvl, brief, f"{m['cmd']}\n\n{m['raw']}{extra}", data=m)
    return _ck("mtu", "链路 MTU", "info", "探测被打断（ICMP 被拦，或链路 MTU < 1328）",
               f"{m['cmd']}\n\n{m['raw']}", data=m)


def _chk_hosts(_app: Any) -> dict[str, Any]:
    h = _hosts()
    if not h.get("ok"):
        return _ck("hosts", "hosts 文件", "warn", "读不到 hosts", str(h.get("error") or ""))
    blocked = h.get("blocked") or []
    bad_n = int(h.get("blocked_n") or len(blocked))
    dup = int(h.get("dup") or 0)
    writable = bool(h.get("writable", True))
    detail = (f"{h['path']}\n有效条目: {h['entries']}　重复: {dup}　"
              f"疑似屏蔽: {bad_n}　可写: {'是' if writable else '否（只读）'}\n\n"
              f"{h.get('raw') or ''}")
    if blocked or not writable or dup:
        bits = []
        if blocked:
            bits.append(f"{bad_n} 条屏蔽（{', '.join(blocked[:3])}…）")
        if not writable:
            bits.append("文件只读")
        if dup:
            bits.append(f"{dup} 条重复")
        return _ck("hosts", "hosts 文件", "warn", "、".join(bits),
                   detail + "\n\nhosts 的优先级最高，命中就永远不走 DNS——"
                            "有些「加速器」和破解补丁会往里写死 IP，导致某些站点直接打不开。"
                            "\n「修复 hosts」会先备份，再只删屏蔽条目、补回标准 localhost、按域名去重，"
                            "你自己写的正常映射一律不动。",
                   fix="fix_hosts", fix_title="备份并修复 hosts", need_admin=True, data=h)
    return _ck("hosts", "hosts 文件", "ok", f"{h['entries']} 条，无屏蔽", detail, data=h)


def _chk_conflict(app: Any) -> dict[str, Any]:
    try:
        pids = set()
        for name in ("_self_pids",):
            fn = getattr(app, name, None)
            if callable(fn):
                try:
                    pids = set(fn() or ())
                except Exception:  # noqa: BLE001
                    pass
        info = netguard.scan(exclude_pids=pids)
    except Exception as e:  # noqa: BLE001
        return _ck("conflict", "代理软件冲突", "skip", f"扫描失败 {type(e).__name__}")
    # 自家内核也叫 mihomo.exe，按安装目录再排一遍，免得把自己报成「别人的代理」
    here = os.path.dirname(os.path.abspath(__file__)).lower()
    info["apps"] = [a for a in (info.get("apps") or [])
                    if not str(a.get("path") or "").lower().startswith(here[:24])]
    apps = info.get("apps") or []
    adaps = info.get("adapters") or []
    detail = ("第三方代理进程:\n  " + ("\n  ".join(f"{a['name']} (pid {a['pid']})" for a in apps) or "无")
              + "\nTUN/TAP 网卡:\n  " + ("\n  ".join(f"{a['name']} · {a['desc']}" for a in adaps) or "无"))
    if apps and app is None:
        # 拿不到自己的 PID 时（例如开发期直接调 probe(None)），同名 mihomo.exe
        # 很可能就是我们在用的内核，不该报成冲突。
        names = {a["name"].lower() for a in apps}
        if names == {"mihomo.exe"}:
            return _ck("conflict", "代理软件冲突", "info",
                       "只看到 mihomo.exe（若是本程序的内核可忽略）",
                       detail + "\n\n程序内部会用自己的 PID 把它排除；这个提示只在没有 PID 上下文时出现。",
                       data=info)
    if apps:
        return _ck("conflict", "代理软件冲突", "warn",
                   f"{len(apps)} 个别的代理进程在跑（{apps[0]['name']}）",
                   detail + "\n\n两套代理同时抢系统代理/路由表，表现是时通时断。要么只用一套，要么清掉它。",
                   data=info)
    if adaps:
        return _ck("conflict", "代理软件冲突", "warn",
                   f"有 {len(adaps)} 张 TUN 网卡挂着（{adaps[0]['name']}）",
                   detail + "\n\nTUN 直接改路由表，关掉系统代理也照样接管全部流量。",
                   data=info)
    return _ck("conflict", "代理软件冲突", "ok", "没有别的代理在抢", detail, data=info)


CHECKS = (_chk_link, _chk_gateway, _chk_dns_cfg, _chk_dns_local, _chk_dns_public,
          _chk_sysproxy, _chk_kernel, _chk_direct, _chk_mtu, _chk_hosts,
          _chk_conflict, _chk_ipv6)

# 一键修复按这个顺序跑：先清缓存/还原代理，再动 DNS，最后才重启内核。
SAFE_FIXES = ("flush_dns", "fix_proxy", "start_dns", "restart_kernel")

FIX_TEXT = {
    "flush_dns": "清空 DNS 缓存",
    "fix_proxy": "清空系统代理（恢复直连）",
    "fix_dns": "把网卡 DNS 固定成公共 DNS",
    "fix_dns_keep_local": "给现有 DNS 补一个公共兜底",
    "dns_auto": "网卡 DNS 改回自动获取",
    "start_dns": "拉起本机解析 127.0.0.1:53",
    "restart_kernel": "重启代理内核",
    "renew_ip": "重新获取 IP（dhcp renew）",
    "restart_adapter": "重启默认网卡",
    "winsock_reset": "重置 Winsock（需重启）",
    "fix_hosts": "修复 hosts（备份 + 去屏蔽 + 补回 localhost）",
    "clean_hosts": "修复 hosts（旧名，等价 fix_hosts）",
    "open_portal": "打开认证页",
}


# ────────────────────────── 体检 ──────────────────────────

def probe(app: Any = None, timeout_total: float = 20.0) -> dict[str, Any]:
    """跑一轮完整体检。单条检查炸了不影响整份报告。"""
    t0 = time.time()
    checks: list[dict[str, Any]] = []
    for fn in CHECKS:
        if time.time() - t0 > timeout_total:
            checks.append(_ck(fn.__name__, fn.__name__, "skip", "超时跳过"))
            continue
        try:
            checks.append(fn(app))
        except Exception as e:  # noqa: BLE001
            log.warning("体检项 %s 失败: %s", getattr(fn, "__name__", "?"), e)
            checks.append(_ck(getattr(fn, "__name__", "?"), getattr(fn, "__name__", "?"),
                              "skip", f"{type(e).__name__}: {e}"))
    bad = [c for c in checks if c["level"] == "bad"]
    warn = [c for c in checks if c["level"] == "warn"]
    level = "bad" if bad else ("warn" if warn else "ok")
    summary = ("全部正常" if level == "ok"
               else "；".join(c["brief"] for c in (bad + warn)[:2]))
    return {
        "ok": True, "level": level, "summary": summary,
        "bad": len(bad), "warn": len(warn),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "is_admin": netguard.is_admin(),
        "checks": checks,
        "fixes": [{"action": a, "title": FIX_TEXT.get(a, a)} for a in SAFE_FIXES],
    }


def check_one(app: Any, cid: str) -> dict[str, Any]:
    """只重跑某一项（修完之后就地刷新用），别让 UI 整轮重来。"""
    for fn in CHECKS:
        name = fn.__name__.replace("_chk_", "")
        if name == cid:
            return fn(app)
    return _ck(cid, cid, "skip", "没有这个检查项")


# ────────────────────────── 修复动作 ──────────────────────────

def _set_adapter_dns(servers: list[str], iface: str = "") -> dict[str, Any]:
    """把 DNS 写到网卡上；iface 为空时挨个试「已连接」的网卡。"""
    if not netguard.is_admin():
        return {"ok": False, "out": "需要管理员权限", "need_admin": True}
    names = [iface] if iface else []
    if not names:
        r = _run(["netsh", "interface", "show", "interface"], timeout=8)
        for line in r["out"].splitlines():
            if "已连接" in line or "Connected" in line:
                name = re.sub(r"^\s*\S+\s+\S+\s+\d+\s+\d+\s+", "", line).strip()
                if name:
                    names.append(name)
    outs = []
    ok_any = False
    for name in names[:3]:
        _run(["netsh", "interface", "ipv4", "set", "dnsservers", f"name={name}",
              "source=static", f"address={servers[0]}", "register=primary"], timeout=8)
        for extra in servers[1:]:
            _run(["netsh", "interface", "ipv4", "add", "dnsservers", f"name={name}",
                  f"address={extra}", "index=2"], timeout=8)
        cur = _dns_servers()
        hit = set(cur["servers"])
        if servers[0] in hit:
            ok_any = True
            outs.append(f"{name}: 已设为 {', '.join(servers)}")
    return {"ok": ok_any, "out": "\n".join(outs) or "没有找到已连接的网卡"}


def fix(app: Any, action: str) -> dict[str, Any]:
    """执行一个修复动作，返回 {ok, steps[], check?}；check 是修完就地复检的结果。"""
    action = (action or "").strip()
    steps: list[dict[str, Any]] = []
    cid = {"fix_proxy": "sysproxy", "fix_dns": "dns_cfg", "fix_dns_keep_local": "dns_cfg",
           "dns_auto": "dns_cfg", "start_dns": "dns_local", "restart_kernel": "kernel",
           "renew_ip": "link", "restart_adapter": "gateway",
           "fix_hosts": "hosts", "clean_hosts": "hosts",
           "flush_dns": "dns_public", "open_portal": "direct"}.get(action, "")

    try:
        if action == "flush_dns":
            r = _run(["ipconfig", "/flushdns"], timeout=10)
            steps.append({"step": "ipconfig /flushdns", "ok": r["ok"], "out": r["out"][:400]})

        elif action == "fix_proxy":
            if hasattr(app, "settings"):
                try:
                    app.settings["system_proxy"] = False
                    app.settings.save()
                    steps.append({"step": "关闭「系统代理」意图", "ok": True, "out": "settings.system_proxy = False"})
                except Exception as e:  # noqa: BLE001
                    steps.append({"step": "关闭「系统代理」意图", "ok": False, "out": str(e)})
            r = netguard.wipe_system_proxy()
            steps.append({"step": "清空 WinINET 代理", "ok": bool(r.get("ok")), "out": str(r)[:400]})
            if hasattr(app, "_set"):
                try:
                    app._set(sysproxy=False)          # noqa: SLF001
                except Exception:  # noqa: BLE001
                    pass

        elif action == "fix_dns":
            r = _set_adapter_dns(list(DNS_FALLBACK[:2]))
            steps.append({"step": "netsh ... set dnsservers", "ok": bool(r.get("ok")),
                          "out": r.get("out", ""), "need_admin": r.get("need_admin", False)})

        elif action == "fix_dns_keep_local":
            cur = _dns_servers()
            keep = [s for s in cur["servers"] if s not in LOOPBACK_DNS][:1]
            servers = (keep or []) + [DNS_FALLBACK[0]]
            r = _set_adapter_dns(servers)
            steps.append({"step": f"补兜底 DNS {servers}", "ok": bool(r.get("ok")),
                          "out": r.get("out", ""), "need_admin": r.get("need_admin", False)})

        elif action == "dns_auto":
            if not netguard.is_admin():
                steps.append({"step": "netsh set dnsservers dhcp", "ok": False,
                              "out": "需要管理员权限", "need_admin": True})
            else:
                r = _run(["netsh", "interface", "ipv4", "set", "dnsservers",
                          "name=Wi-Fi", "source=dhcp"], timeout=8)
                steps.append({"step": "网卡 DNS 改回自动", "ok": r["ok"], "out": r["out"][:300]})

        elif action == "start_dns":
            fn = getattr(app, "set_dns_running", None) or getattr(app, "_set_dns", None)
            if callable(fn):
                res = fn(True)
                steps.append({"step": "启动本机解析", "ok": bool(res) if isinstance(res, bool) else True,
                              "out": str(res)[:300]})
            elif dnsguard.port_busy(53):
                steps.append({"step": "启动本机解析", "ok": True, "out": "53 已有服务在听"})
            else:
                steps.append({"step": "启动本机解析", "ok": False,
                              "out": "程序里没有可用的启动入口；到「扩展 → 本机解析」打开开关"})

        elif action == "restart_kernel":
            fn = getattr(app, "restart_kernel", None)
            if callable(fn):
                res = fn()
                steps.append({"step": "重启内核", "ok": bool(res) if isinstance(res, bool) else True,
                              "out": str(res)[:300]})
            else:
                steps.append({"step": "重启内核", "ok": False, "out": "没有 restart_kernel 接口"})

        elif action == "renew_ip":
            r1 = _run(["ipconfig", "/release"], timeout=12)
            r2 = _run(["ipconfig", "/renew"], timeout=25)
            steps.append({"step": "ipconfig /release", "ok": r1["ok"], "out": r1["out"][-300:]})
            steps.append({"step": "ipconfig /renew", "ok": r2["ok"], "out": r2["out"][-400:]})

        elif action == "restart_adapter":
            if not netguard.is_admin():
                steps.append({"step": "禁用/启用网卡", "ok": False, "out": "需要管理员权限",
                              "need_admin": True})
            else:
                iface = _default_iface_name()
                if not iface:
                    steps.append({"step": "禁用/启用网卡", "ok": False, "out": "没找到默认网卡"})
                else:
                    d = _run(["netsh", "interface", "set", "interface", f"name={iface}",
                              "admin=disable"], timeout=12)
                    time.sleep(1.5)
                    e = _run(["netsh", "interface", "set", "interface", f"name={iface}",
                              "admin=enable"], timeout=12)
                    steps.append({"step": f"重启网卡 {iface}", "ok": e["ok"],
                                  "out": f"disable rc={d['code']} / enable rc={e['code']}"})

        elif action == "winsock_reset":
            if not netguard.is_admin():
                steps.append({"step": "netsh winsock reset", "ok": False,
                              "out": "需要管理员权限", "need_admin": True})
            else:
                r1 = _run(["netsh", "winsock", "reset"], timeout=20)
                r2 = _run(["netsh", "int", "ip", "reset"], timeout=20)
                steps.append({"step": "netsh winsock reset", "ok": r1["ok"], "out": r1["out"][-300:]})
                steps.append({"step": "netsh int ip reset", "ok": r2["ok"], "out": r2["out"][-300:]})
                steps.append({"step": "需要重启电脑才生效", "ok": True, "out": ""})

        elif action in ("fix_hosts", "clean_hosts"):
            steps.append(fix_hosts())

        elif action == "open_portal":
            import webbrowser
            url = "http://connect.rom.miui.com/generate_204"
            try:
                webbrowser.open(url)
                steps.append({"step": "打开认证页", "ok": True, "out": url})
            except Exception as e:  # noqa: BLE001
                steps.append({"step": "打开认证页", "ok": False, "out": str(e)})

        else:
            return {"ok": False, "error": f"未知修复动作：{action}", "steps": []}
    except Exception as e:  # noqa: BLE001
        log.warning("修复动作 %s 异常: %s", action, e)
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "steps": steps}

    ok = any(s.get("ok") for s in steps) if steps else False
    need_admin = any(s.get("need_admin") for s in steps)
    out = {"ok": ok, "action": action, "title": FIX_TEXT.get(action, action),
           "steps": steps, "need_admin": need_admin,
           "at": time.strftime("%H:%M:%S")}
    if cid:
        try:
            out["check"] = check_one(app, cid)
        except Exception as e:  # noqa: BLE001
            log.debug("复检 %s 失败: %s", cid, e)
    return out


def _default_iface_name() -> str:
    """从默认网关反查网卡名（给重启网卡用）。"""
    gw = str(hotspot.identity().get("gw") or "")
    if not gw:
        return ""
    r = _run(["powershell", "-NoProfile", "-Command",
              "(Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway.NextHop -eq '"
              + gw + "'} | Select-Object -First 1).InterfaceAlias"], timeout=12)
    return (r["out"] or "").strip().splitlines()[-1].strip() if r["out"].strip() else ""


def _hosts_backup(path: str) -> str:
    """备份成带时间戳的名字，同时留一份固定名方便用户自己还原。"""
    import shutil
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"{path}.vpntray-{stamp}.bak"
    shutil.copy2(path, bak)
    try:
        shutil.copy2(path, path + ".vpntray.bak")
    except OSError:
        pass
    return bak


def fix_hosts(apply: bool = True, admin: bool | None = None) -> dict[str, Any]:
    """修 hosts：备份 → 删屏蔽 → 补回标准回环 → 按域名去重 → flushdns。

    apply=False 只算不写（界面预览和单测都用它）；admin=None 时自己查权限。
    只删「把域名指到 0.0.0.0 / 127.0.0.1 / ::1 / ::」的行，自定义的正常映射原样保留。
    """
    h = _hosts()
    if not h.get("ok"):
        return {"step": "修复 hosts", "ok": False, "out": str(h.get("error") or "")}
    path = h["path"]
    if apply and admin is None:
        admin = netguard.is_admin()
    if apply and not admin:
        return {"step": "修复 hosts", "ok": False, "need_admin": True,
                "out": "需要管理员权限（hosts 在 System32\\drivers\\etc 下）"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {"step": "修复 hosts", "ok": False, "out": f"{type(e).__name__}: {e}"}

    kept: list[str] = []
    dropped: list[str] = []
    merged = 0
    seen: set[str] = set()
    for ln in lines:
        body = ln.split("#", 1)[0].strip()
        if not body:
            kept.append(ln.rstrip())            # 空行和纯注释原样保留
            continue
        parts = body.split()
        if len(parts) < 2:
            kept.append(ln.rstrip())
            continue
        ip, names = parts[0], parts[1:]
        if ip in BLOCK_IPS:
            bad = [n for n in names if n.lower() not in LOCAL_NAMES]
            good = [n for n in names if n.lower() in LOCAL_NAMES]
            dropped.extend(bad)
            if good:                            # 127.0.0.1 localhost 这类标准行留着
                for n in good:
                    seen.add(n.lower())
                kept.append(ip + "  " + "  ".join(good))
            continue
        uniq: list[str] = []
        for n in names:
            if n.lower() in seen:
                merged += 1
                continue
            seen.add(n.lower())
            uniq.append(n)
        if uniq:
            kept.append(ip + "  " + "  ".join(uniq))
        else:
            merged += len(names)                # 整行都是重复 → 算合并，不算屏蔽

    head = [f"# VPNTray 修复于 {time.strftime('%Y-%m-%d %H:%M:%S')}："
            f"删掉 {len(dropped)} 条屏蔽条目，合并 {merged} 条重复",
            "# 要还原就找同目录下的 hosts.vpntray-*.bak"]
    for std in STD_HOSTS_LINES:
        if std.split()[1].lower() not in seen:
            head.append(std)
    text = "\r\n".join(head + [""] + kept).rstrip("\r\n") + "\r\n"

    if not apply:
        return {"step": "修复 hosts（预览）", "ok": True, "dry_run": True,
                "out": text[:1800], "dropped": dropped[:40], "merged": merged, "path": path}

    bak = ""
    try:
        bak = _hosts_backup(path)
        if not os.access(path, os.W_OK):
            os.chmod(path, 0o666)               # 被破解补丁设成只读的老坑
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    except OSError as e:
        return {"step": "修复 hosts", "ok": False, "out": f"{type(e).__name__}: {e}",
                "backup": bak, "path": path}
    r = _run(["ipconfig", "/flushdns"], timeout=10)
    return {"step": "修复 hosts", "ok": True, "path": path, "backup": bak,
            "dropped": dropped[:40], "merged": merged,
            "out": (f"备份 {bak}\n删除屏蔽 {len(dropped)} 条："
                    f"{', '.join(dropped[:5]) or '无'}\n合并重复 {merged} 条\n"
                    f"补回标准回环 localhost\nflushdns rc={r['code']}")}


def _clean_hosts() -> dict[str, Any]:
    """老名字保留，等价于 fix_hosts()（2.6/3.0 的调用方和测试都还在用）。"""
    return fix_hosts()


def repair(app: Any, actions: list[str] | None = None) -> dict[str, Any]:
    """一键修复：默认只跑安全集合（清缓存 → 还原代理 → 拉解析 → 重启内核）。

    需要管理员/会断链的动作（禁用网卡、winsock reset、改 DNS）不自动跑，
    只把「还差什么」列出来，让用户自己按 —— 这个程序绝不替用户决定断网三秒。
    """
    todo = [a for a in (actions or SAFE_FIXES) if a in FIX_TEXT]
    steps: list[dict[str, Any]] = []
    for a in todo:
        steps.append(fix(app, a))
    before_level = None
    try:
        before_level = (probe(app) or {}).get("level")
    except Exception:  # noqa: BLE001
        pass
    ok = any(s.get("ok") for s in steps)
    pending = sorted({s.get("need_admin") and s.get("action") for s in steps if s.get("need_admin")})
    return {"ok": ok, "steps": steps, "level_after": before_level,
            "pending_admin": [a for a in pending if a],
            "at": time.strftime("%H:%M:%S")}


# ────────────────────────── 文本报告 ──────────────────────────

_ICON = {"ok": "[OK]  ", "warn": "[WARN]", "bad": "[BAD] ", "info": "[--]  ", "skip": "[SKIP]"}


def report_text(res: dict[str, Any]) -> str:
    """专业详情：能给客服/同事直接贴的那一份。"""
    if not isinstance(res, dict) or "checks" not in res:
        return "（还没有体检结果，先点一次「一键体检」）"
    lines = [
        "VPNTray 网络体检 " + str(res.get("at") or ""),
        f"结论: {res.get('level')} · {res.get('summary')}",
        f"耗时: {res.get('elapsed_ms')}ms · 管理员: {'是' if res.get('is_admin') else '否'}",
        "-" * 56,
    ]
    for c in res.get("checks") or []:
        lines.append(f"{_ICON.get(c.get('level'), '[?]')} {c.get('title')}: {c.get('brief')}")
        detail = str(c.get("detail") or "").strip()
        if detail:
            for ln in detail.splitlines():
                lines.append("        " + ln)
        if c.get("fix"):
            lines.append(f"        修法: {c.get('fix_title')}（{c.get('fix')}）"
                         + ("  需管理员" if c.get("need_admin") else ""))
    return "\n".join(lines)


__all__ = ["probe", "fix", "repair", "check_one", "report_text", "CHECKS",
           "SAFE_FIXES", "FIX_TEXT", "DNS_FALLBACK"]
