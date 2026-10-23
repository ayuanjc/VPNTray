"""移动热点特化：链路识别、地区策略、mihomo 参数档位。

针对「手机个人热点（iPhone / 安卓）+ 运营商移动网络」这一组合做的适配。
三个真实痛点：

1. iPhone 热点是 172.20.10.0/28 的 NAT，UDP 和大包最先出问题，且每次重连
   本地 IP 都会变（.2 ~ .14），mihomo 的出口绑定就失效了；
2. 移动网络的 QoS 按端口和并发握手数掐，tcp-concurrent 开着容易在
   系统代理全量走 7891 时把自己掐死；
3. OpenAI 封香港落地 IP，香港节点上 api.openai.com 表现为
   SSL EOF / TLS 被重置，而 google / github 全通 —— 换个香港节点没用，
   必须换地区。这件事不能靠「延迟低」判断，只能靠地区名单。
"""
from __future__ import annotations

import logging
import re
import socket
import subprocess
import time
from typing import Any

log = logging.getLogger("hotspot")

# ── 已知手机热点网段 ──
# iPhone（个人热点 / 即时热点）固定 172.20.10.0/28；
# 安卓 USB 共享常见 192.168.42.0/24，Wi-Fi 共享常见 192.168.43.0/24。
IPHONE_NET = ("172.20.10.", 28)
ANDROID_NETS = ("192.168.42.", "192.168.43.")

# ── 地区识别 ──
REGIONS = [
    "香港", "澳门", "台湾", "台北", "日本", "东京", "大阪", "新加坡", "狮城",
    "美国", "洛杉矶", "圣何塞", "西雅图", "韩国", "首尔", "马来", "吉隆坡",
    "越南", "泰国", "印度", "德国", "英国", "法国", "荷兰", "土耳其",
    "迪拜", "俄罗斯", "加拿大", "澳大利亚", "菲律宾", "印尼", "巴西",
]
REGION_ALIAS = {
    "HK": "香港", "HongKong": "香港", "Hong Kong": "香港",
    "TW": "台湾", "Taiwan": "台湾",
    "JP": "日本", "Japan": "日本",
    "SG": "新加坡", "Singapore": "新加坡",
    "US": "美国", "USA": "美国", "United States": "美国",
    "KR": "韩国", "Korea": "韩国",
}

# 端点侧真的在拒绝的地区（延迟再低也没用）
# 澳门是实测踩出来的：🇲🇴|澳门-IEPL 延迟最低（171ms），但 Codex 报
# 「澳门不在 codex 的服务范围」。延迟低和能用是两件事。
DEFAULT_BLOCK = [
    "香港", "澳门", "中国大陆", "中国", "俄罗斯", "伊朗", "朝鲜", "古巴", "叙利亚", "白俄罗斯",
]

_cache: dict[str, Any] = {"at": 0.0, "ip": "", "gw": "", "kind": "unknown"}


def local_ip() -> str:
    """取本机在当前链路上的出口网卡地址（不发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.6)
        s.connect(("223.5.5.5", 53))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


def default_gateway() -> str:
    """解析默认网关。只认第一段 IPv4，中英文 ipconfig 都覆盖。"""
    try:
        out = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=6,
        ).stdout
    except Exception:  # noqa: BLE001
        return ""
    for enc in ("gbk", "utf-8"):
        try:
            text = out.decode(enc, "replace")
        except Exception:  # noqa: BLE001
            continue
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            low = ln.lower()
            if ("default gateway" in low or "默认网关" in ln) and ":" in ln:
                # 值可能在同一行，也可能在下一行（Windows 的中文排版）
                tail = ln.split(":", 1)[1].strip()
                cand = tail or (lines[i + 1].strip() if i + 1 < len(lines) else "")
                m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", cand)
                if m and m.group(1) != "0.0.0.0":
                    return m.group(1)
        if text:
            break
    return ""


def kind_of(ip: str) -> str:
    if not ip:
        return "unknown"
    if ip.startswith(IPHONE_NET[0]):
        return "iphone_hotspot"
    for net in ANDROID_NETS:
        if ip.startswith(net):
            return "android_hotspot"
    if ip.startswith(("192.168.", "10.", "172.16.", "172.17.", "172.18.",
                      "172.19.", "172.2", "172.30.", "172.31.")):
        return "lan"
    return "unknown"


def identity(refresh: bool = False) -> dict[str, Any]:
    """当前链路身份。带 15 秒缓存，看护循环里每轮调也不会拖慢。"""
    now = time.time()
    if not refresh and now - float(_cache["at"] or 0) < 15:
        return dict(_cache)
    ip = local_ip()
    gw = default_gateway()
    _cache.update(at=now, ip=ip, gw=gw, kind=kind_of(ip))
    return dict(_cache)


def net_id() -> tuple[str, str]:
    """链路指纹：热点重连后本地 IP 一定变，用它判断「网络换了」。"""
    info = identity(refresh=True)
    return (str(info.get("gw") or ""), str(info.get("ip") or ""))


def is_hotspot() -> bool:
    k = kind_of(local_ip())
    return k in ("iphone_hotspot", "android_hotspot")


# ── 地区策略 ──

def region_of(name: str) -> str:
    n = name or ""
    for word in REGIONS:
        if word in n:
            return word
    for alias, word in REGION_ALIAS.items():
        if re.search(r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])", n):
            return word
    return ""


def is_blocked_region(name: str, settings=None) -> bool:
    """名字是否属于「OpenAI 会拒绝」的地区。"""
    words = None
    if settings is not None:
        try:
            words = settings.get("ai_block_regions")
        except Exception:  # noqa: BLE001
            words = None
    words = list(words or DEFAULT_BLOCK)
    reg = region_of(name)
    for w in words:
        if not w:
            continue
        if w == reg or w in (name or ""):
            return True
    return False


def region_rank(name: str, settings=None) -> int:
    """越小越优先：先按偏好顺序，再看是不是被封锁地区。"""
    pref: list[str] = []
    if settings is not None:
        try:
            pref = list(settings.get("ai_prefer_regions") or [])
        except Exception:  # noqa: BLE001
            pref = []
    reg = region_of(name)
    for i, w in enumerate(pref):
        if w and (w == reg or w in (name or "")):
            return i
    return 50 if is_blocked_region(name, settings) else 20


# ── mihomo 参数档位 ──

def profile(settings=None) -> dict[str, Any]:
    """按链路返回 mihomo 参数。热点档更保守：

    - tcp-concurrent 关：并发握手在移动网络 QoS 下反而丢包更狠；
    - find-process-mode off：热点下 WinDivert/进程匹配经常拿不到归属，
      白耗 CPU，还会拖慢首包；
    - keep-alive-interval 拉长：移动网络中途 NAT 超时很频繁，
      保活太勤会被当成扫描。
    """
    aware = True
    if settings is not None:
        try:
            aware = bool(settings.get("mobile_aware", True))
        except Exception:  # noqa: BLE001
            aware = True
    if aware and is_hotspot():
        return {
            "tcp-concurrent": False,
            "find-process-mode": "off",
            "keep-alive-interval": 60,
            "tier": "hotspot",
        }
    return {
        "tcp-concurrent": True,
        "find-process-mode": "strict",
        "keep-alive-interval": 15,
        "tier": "normal",
    }


def describe(settings=None) -> str:
    info = identity()
    prof = profile(settings)
    k = info.get("kind")
    label = {
        "iphone_hotspot": "iPhone 个人热点",
        "android_hotspot": "安卓热点",
        "lan": "局域网/WiFi",
    }.get(str(k), "未知链路")
    return (f"{label}  本机 {info.get('ip') or '-'}  网关 {info.get('gw') or '-'}  "
            f"档位 {prof['tier']}")
