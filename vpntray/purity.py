"""IP 纯净度检测。

原理：把流量切到目标节点，从国内看「这个节点的出口 IP 长什么样」——
机房 IP / 被标记为代理 / 被标记 VPN / 被滥用 / 落地国家与标称不符，
都会影响流媒体和 ChatGPT 这类服务的可用性。

同时给出真实带宽（下载测速），用于按「网速」排序。

结果缓存在 data/purity.json，重启后仍然有效。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import requests

from .paths import DATA, ensure_dirs

_lock = threading.Lock()

log = logging.getLogger("purity")

CACHE = DATA / "purity.json"
CACHE_TTL = 24 * 3600  # 单节点结果保留 24 小时

FLAG_CC = {
    "🇭🇰": "HK", "🇸🇬": "SG", "🇹🇼": "TW", "🇯🇵": "JP", "🇰🇷": "KR", "🇲🇴": "MO",
    "🇬🇧": "GB", "🇫🇷": "FR", "🇩🇪": "DE", "🇮🇹": "IT", "🇳🇴": "NO", "🇺🇸": "US",
    "🇨🇦": "CA", "🇦🇺": "AU", "🇺🇦": "UA", "🇹🇷": "TR", "🇦🇪": "AE", "🇳🇬": "NG",
    "🇵🇭": "PH", "🇹🇭": "TH", "🇻🇳": "VN", "🇲🇾": "MY", "🇮🇳": "IN", "🇮🇩": "ID",
    "🇦🇷": "AR", "🇧🇷": "BR", "🇨🇳": "CN", "🇷🇺": "RU", "🇳🇱": "NL", "🇪🇸": "ES",
    "🇸🇪": "SE", "🇨🇭": "CH", "🇵🇱": "PL", "🇮🇪": "IE", "🇫🇮": "FI", "🇩🇰": "DK",
    "🇦🇹": "AT", "🇧🇪": "BE", "🇨🇿": "CZ", "🇬🇷": "GR", "🇮🇱": "IL", "🇸🇦": "SA",
    "🇿🇦": "ZA", "🇲🇽": "MX", "🇨🇱": "CL", "🇳🇿": "NZ", "🇵🇰": "PK", "🇧🇩": "BD",
}

# ────────────────────────── 查询 ──────────────────────────
def _session(port: int) -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    p = f"http://127.0.0.1:{port}"
    s.proxies = {"http": p, "https": p}
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    return s


# ────────────────────────── One-IP ──────────────────────────
# One-IP（github.com/zhihui-hu/one-ip）的纯净度数据来自 ip.net.coffee 的
# trust_score + 一组布尔标记（住宅/机房/VPN/代理/Tor/爬虫/滥用）。
#
# 两个必须记住的实测结论：
#   1. ip.net.coffee **只能直连**。走本地代理会稳定 SSL EOF（代理出口到它
#      的 TLS 握不上），实测两次都是 SSLEOFError。
#   2. 所以「拿出口 IP」和「查纯净度」是两条不同的路：IP 必须走代理问
#      国外站（走代理问国内站会被规则判 DIRECT，拿到的是本机真实 IP），
#      纯净度则拿这个 IP 直连 net.coffee 查。
ONEIP_HOST = os.environ.get("VPNTRAY_ONEIP_HOST", "https://ip.net.coffee").rstrip("/")
ONEIP_MIN_SCORE = 45   # One-IP 官方分档：>=75 good / 45-74 moderate / <45 poor


def _oneip(ip: str, timeout: float = 8.0) -> dict[str, Any] | None:
    """直连 One-IP 数据源，查指定 IP 的画像。失败返回 None，交给上面的兜底。"""
    url = f"{ONEIP_HOST}/api/ip/lookup/{ip}"
    try:
        # 直连：trust_env=False 且不设 proxies，避免被系统代理/HKLM 带偏
        with requests.Session() as s:
            s.trust_env = False
            s.proxies = {}
            s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with s.get(url, timeout=timeout) as r:
                if r.status_code != 200:
                    log.debug("one-ip HTTP %s", r.status_code)
                    return None
                d = r.json()
    except Exception as e:  # noqa: BLE001
        log.debug("one-ip 失败: %s", e)
        return None
    if not isinstance(d, dict) or str(d.get("ip") or "") != ip:
        return None
    return d


def _from_oneip(ip: str, d: dict[str, Any]) -> dict[str, Any]:
    """把 net.coffee 的字段摊成 score() 认识的 ipapi.is 形状。"""
    # net.coffee 用 is_datacenter 的布尔，company_type 则是 hosting/isp/mixed
    ctype = str(d.get("company_type") or "").lower()
    hosting = bool(d.get("is_datacenter")) or ctype == "hosting"
    ts = d.get("trust_score")
    trust = ts if isinstance(ts, (int, float)) and 0 <= float(ts) <= 100 else None
    return {
        "ip": ip,
        "is_proxy": d.get("is_proxy"),
        "is_vpn": d.get("is_vpn"),
        "is_tor": d.get("is_tor"),
        "is_crawler": d.get("is_crawler"),
        "is_abuser": d.get("is_abuser"),
        "is_datacenter": d.get("is_datacenter"),
        "is_mobile": d.get("is_mobile"),
        "is_bogon": d.get("is_bogon"),
        "is_residential": d.get("isResidential"),
        # One-IP 自己的 0-100 信誉分，比本地扣分制权威，score() 会优先用它
        "trust_score": trust,
        "abuser_score": d.get("abuser_score"),
        "reddit_blocked": d.get("reddit_blocked"),
        "ai_verdict": d.get("ai_verdict") if isinstance(d.get("ai_verdict"), dict) else None,
        "datacenter": {"name": d.get("datacenter_name") or ""} if d.get("datacenter_name") else None,
        "location": {
            "country": d.get("country"),
            "country_code": d.get("countryCode") or d.get("country"),
            "city": d.get("city") or d.get("region") or "",
            # net.coffee 不给经纬度；这里留空，由 ip-api 那一路补
            "latitude": None,
            "longitude": None,
        },
        "asn": {
            "org": d.get("asOrganization") or d.get("asname") or "",
            "asn": d.get("asn"),
            "type": "hosting" if hosting else "isp",
        },
        "company": {
            "name": d.get("company_name") or d.get("asOrganization") or "",
            "type": "hosting" if hosting else (ctype or "isp"),
        },
        "_src": "one-ip",
    }


def fetch_exit_info(port: int, timeout: float = 9.0) -> dict[str, Any] | None:
    """通过本地代理查「当前出口 IP」的画像。

    Session 必须关：每检测一个节点建一个，100 个节点两轮下来就是两百个
    连接池一直挂着，句柄数会一路涨到系统报警。
    """
    with _session(port) as s:
        # 第一步：走代理问 ip-api 拿出口 IP + 经纬度（One-IP 没有坐标）
        # ip-api 免费版只支持 http，45 次/分钟。它是国外站，走代理才拿到节点
        # 出口；换成国内的回显服务会被规则判 DIRECT，拿到的是本机 IP。
        base: dict[str, Any] = {}
        ip = ""
        try:
            r = s.get(
                "http://ip-api.com/json/?fields=status,query,countryCode,city,isp,org,as,asname,"
                "proxy,hosting,mobile,lat,lon",
                timeout=timeout,
            )
            d = r.json() if r.status_code == 200 else {}
            if isinstance(d, dict) and d.get("status") == "success" and d.get("query"):
                ip = str(d["query"])
                base = d
        except Exception as e:  # noqa: BLE001
            log.debug("ip-api 失败: %s", e)

        # 第二步：拿这个出口 IP 直连 One-IP 查纯净度（trust_score + flags）
        one = _oneip(ip, timeout=min(timeout, 8.0)) if ip else None
        if one:
            info = _from_oneip(ip, one)
            # 坐标只有 ip-api 有，补进去；其余字段一律以 One-IP 为准
            loc = info["location"]
            loc["latitude"] = base.get("lat")
            loc["longitude"] = base.get("lon")
            if not loc.get("city"):
                loc["city"] = base.get("city") or ""
            if not loc.get("country_code"):
                loc["country_code"] = base.get("countryCode")
            return info

        # 兜底 1：ipapi.is（字段同样很全，但经常 403 / 超时）
        try:
            r = s.get("https://api.ipapi.is/", timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("ip"):
                    d["_src"] = "ipapi.is"
                    return d
        except Exception as e:  # noqa: BLE001
            log.debug("ipapi.is 失败: %s", e)

        # 兜底 2：只有 ip-api 拿到的东西，flags 会缺口，分只能照着扣
        if base:
            hosting = bool(base.get("hosting"))
            return {
                "ip": ip,
                "is_proxy": bool(base.get("proxy")),
                "is_vpn": False,
                "is_tor": False,
                "is_abuser": False,
                "is_datacenter": hosting,
                "is_mobile": bool(base.get("mobile")),
                "location": {
                    "country_code": base.get("countryCode"),
                    "city": base.get("city"),
                    "latitude": base.get("lat"),
                    "longitude": base.get("lon"),
                },
                "asn": {"org": base.get("asname") or base.get("as"), "type": "hosting" if hosting else "isp"},
                "company": {"name": base.get("org") or base.get("isp"), "type": "hosting" if hosting else "isp"},
                "_src": "ip-api",
            }

    return None


# ────────────────────────── 评分 ──────────────────────────
def score(info: dict[str, Any], node_name: str) -> dict[str, Any]:
    """0–100 的纯净度分，越高越干净。

    分两种算路：
      · 有 One-IP 的 trust_score → 直接用它的分。那是多源聚合出来的结论
        （机房/代理/VPN/Tor/滥用/爬虫都算进去了），比这里按标记逐项扣分
        更接近各家平台的实际判定。本地只补一条它看不到的：节点名标称的
        国家和实际落地区是否对得上。
      · 没有 → 退回按标记扣分的老算法（ipapi.is / ip-api 兜底时走这条）。
    """
    if not isinstance(info, dict):
        info = {}

    # 缓存里的这三块可能被历史版本写成字符串 / null，直接 .get 会 AttributeError，
    # 而 score 是在渲染线程里跑的 —— 一次抛错整张节点表就白了
    loc = info.get("location") if isinstance(info.get("location"), dict) else {}
    asn = info.get("asn") if isinstance(info.get("asn"), dict) else {}
    comp = info.get("company") if isinstance(info.get("company"), dict) else {}
    cc = str(loc.get("country_code") or loc.get("country") or "").upper()
    expect = FLAG_CC.get(str(node_name or '')[:2], "")

    is_hosting = bool(
        info.get("is_datacenter")
        or str(comp.get("type", "")).lower() == "hosting"
        or str(asn.get("type", "")).lower() == "hosting"
    )
    # ipapi.is 的 datacenter 有时是对象、有时直接是机房名字符串
    dc_obj = info.get("datacenter")
    dc = (dc_obj.get("datacenter") or dc_obj.get("name") or "") if isinstance(dc_obj, dict) else str(dc_obj or "")

    _t = info.get("trust_score")
    trust = int(round(float(_t))) if isinstance(_t, (int, float)) and 0 <= float(_t) <= 100 else None

    tags: list[str] = []
    if trust is not None:
        s = trust
        for key, tag in (
            ("is_tor", "Tor出口"),
            ("is_abuser", "被滥用"),
            ("is_proxy", "代理标记"),
            ("is_vpn", "VPN标记"),
            ("is_crawler", "爬虫库"),
            ("is_bogon", "保留地址"),
        ):
            if info.get(key):
                tags.append(tag)
        if is_hosting:
            # 机房名可能是一整串公司名（"ALLIANCE INTELLIGENT TECHNOLOGIES PTE. LTD."），
            # 标签栏两三个字就够认了，长的直接截断
            dc_short = (dc[:18] + "…") if len(dc) > 18 else dc
            tags.append(f"机房{('/' + dc_short) if dc_short else ''}")
        elif info.get("is_residential") or info.get("is_mobile") or str(comp.get("type", "")).lower() == "isp":
            tags.append("住宅/ISP")
        elif comp.get("type"):
            tags.append(str(comp["type"]))
    else:
        s = 100
        if info.get("is_tor"):
            s -= 45; tags.append("Tor出口")
        if info.get("is_abuser"):
            s -= 40; tags.append("被滥用")
        if info.get("is_proxy"):
            s -= 28; tags.append("代理标记")
        if info.get("is_vpn"):
            s -= 22; tags.append("VPN标记")
        if info.get("is_crawler"):
            s -= 15; tags.append("爬虫库")
        if info.get("is_bogon"):
            s -= 30; tags.append("保留地址")
        if is_hosting:
            s -= 30
            tags.append(f"机房{('/' + dc) if dc else ''}")
        elif str(comp.get("type", "")).lower() == "isp" or info.get("is_mobile"):
            tags.append("住宅/ISP")
        elif comp.get("type"):
            tags.append(str(comp["type"]))

    mismatch = bool(expect and cc and expect != cc)
    if mismatch:
        s -= 18
        tags.append(f"落地≠标称({cc})")

    # One-IP 的附加信号：Reddit 封不封这个段，以及它自己的 AI 判读
    if info.get("reddit_blocked"):
        tags.append("Reddit拦截")
    av = info.get("ai_verdict")
    if isinstance(av, dict) and av.get("label"):
        lb = str(av["label"]).strip()
        if lb and lb.lower() not in ("unknown", "n/a"):
            tags.append(f"AI:{lb[:10]}")

    s = max(0, min(100, int(s)))
    if s >= 85:
        grade, color = "极洁净", "ok"
    elif s >= 75:
        grade, color = "良好", "good"
    elif s >= 45:
        grade, color = "一般", "mid"
    else:
        grade, color = "较差", "bad"

    return {
        "score": s,
        "grade": grade,
        "color": color,
        "tags": tags[:5],
        "ip": info.get("ip"),
        "cc": cc,
        "lat": _f(loc.get("latitude") if loc.get("latitude") is not None else loc.get("lat")),
        "lon": _f(loc.get("longitude") if loc.get("longitude") is not None else loc.get("lon")),
        "asn": (asn.get("org") or asn.get("asn") or "")[:40],
        "company": (comp.get("name") or "")[:40],
        "city": loc.get("city") or "",
        "is_hosting": is_hosting,
        "mismatch": mismatch,
        "src": info.get("_src", ""),
        "trust": trust,
        "ts": time.time(),
    }


def _f(v: Any) -> float | None:
    """宽松转 float，拿不到就是 None。"""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if f != f or abs(f) > 1000:  # NaN / 明显越界
        return None
    return round(f, 4)


# ────────────────────────── 缓存 ──────────────────────────
def _load() -> dict[str, Any]:
    if not CACHE.exists():
        return {}
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
        raise ValueError("缓存根节点不是对象")
    except Exception as e:  # noqa: BLE001
        # 读坏了不能直接当空：下一次 put 会把整份缓存覆盖成「只有 1 条」，
        # 之前测过的几十个节点就白跑了。先留一份 .bad 副本再说。
        log.warning("纯净度缓存损坏（%s），已另存 %s.bad", e, CACHE.name)
        try:
            CACHE.with_name(CACHE.name + ".bad").write_bytes(CACHE.read_bytes())
        except OSError:
            pass
        return {}


def _save(d: dict[str, Any]) -> None:
    """原子写：崩在中间会让整份缓存变成半截 JSON，之后 load_all 直接放弃全部数据。"""
    ensure_dirs()
    try:
        tmp = CACHE.with_name(CACHE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CACHE)
    except Exception as e:  # noqa: BLE001
        log.warning("写入纯净度缓存失败: %s", e)
        try:
            CACHE.with_name(CACHE.name + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass


def load_all() -> dict[str, Any]:
    """返回未过期的缓存。单条脏数据只丢这一条，不能连坐整份缓存。"""
    now = time.time()
    out: dict[str, Any] = {}
    for k, v in _load().items():
        if not isinstance(v, dict):
            continue
        try:
            ts = float(v.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if now - ts < CACHE_TTL:
            out[str(k)] = v
    return out


def put(name: str, data: dict[str, Any]) -> None:
    with _lock:
        d = _load()
        now = time.time()

        def _stale(v: Any) -> bool:
            if not isinstance(v, dict):
                return True
            try:
                return (now - float(v.get("ts") or 0)) >= CACHE_TTL * 3
            except (TypeError, ValueError):
                return True

        # 顺手剔掉早就不用的条目：这份缓存只增不减，跑几个月就是几万个节点名
        for k in [k for k, v in d.items() if _stale(v)]:
            d.pop(k, None)
        d[str(name)] = data
        _save(d)


def clear() -> None:
    if CACHE.exists():
        try:
            CACHE.unlink()
        except OSError:
            pass


# ────────────────────────── 带宽测速 ──────────────────────────
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=5242880"  # 5 MB


def measure_speed(port: int, timeout: float = 25.0) -> dict[str, Any] | None:
    """下载 5MB 估算 Mbps。失败返回 None。

    timeout 是总时长上限：机场的 trickle 节点会「每 24 秒挤一点」，只靠
    requests 的分块读超时永远打不断，这里按累计时间硬截。
    """
    t0 = time.time()
    got = 0
    with _session(port) as s:
        try:
            with s.get(SPEED_URL, stream=True, timeout=timeout) as r:
                if r.status_code != 200:
                    return None
                for chunk in r.iter_content(65536):
                    if not chunk:
                        break
                    got += len(chunk)
                    if time.time() - t0 > timeout:
                        log.debug("测速超时截断，已收 %d 字节", got)
                        break
        except Exception as e:  # noqa: BLE001
            log.debug("测速失败: %s", e)
            if got < 65536:
                return None
    dt = max(0.05, time.time() - t0)
    if got < 20480:
        return None
    mbps = round((got * 8) / dt / 1_000_000, 1)
    return {"mbps": mbps, "bytes": got, "sec": round(dt, 2), "ts": time.time()}
