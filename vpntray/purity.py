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


def fetch_exit_info(port: int, timeout: float = 9.0) -> dict[str, Any] | None:
    """通过本地代理查「当前出口 IP」的画像。

    Session 必须关：每检测一个节点建一个，100 个节点两轮下来就是两百个
    连接池一直挂着，句柄数会一路涨到系统报警。
    """
    with _session(port) as s:
        # 首选 ipapi.is：字段最全（机房/代理/VPN/滥用/公司类型）
        try:
            r = s.get("https://api.ipapi.is/", timeout=timeout)
            if r.status_code == 200:
                d = r.json()
                if isinstance(d, dict) and d.get("ip"):
                    d["_src"] = "ipapi.is"
                    return d
        except Exception as e:  # noqa: BLE001
            log.debug("ipapi.is 失败: %s", e)

        # 兜底 ip-api.com（免费版只支持 http，45 次/分钟）
        try:
            r = s.get(
                "http://ip-api.com/json/?fields=status,query,countryCode,city,isp,org,as,asname,"
                "proxy,hosting,mobile,lat,lon",
                timeout=timeout,
            )
            d = r.json() if r.status_code == 200 else {}
            if not isinstance(d, dict):
                d = {}
            if d.get("status") == "success" and d.get("query"):
                return {
                    "ip": d["query"],
                    "is_proxy": bool(d.get("proxy")),
                    "is_vpn": False,
                    "is_tor": False,
                    "is_abuser": False,
                    "is_datacenter": bool(d.get("hosting")),
                    "is_mobile": bool(d.get("mobile")),
                    "location": {
                        "country_code": d.get("countryCode"),
                        "city": d.get("city"),
                        "latitude": d.get("lat"),
                        "longitude": d.get("lon"),
                    },
                    "asn": {"org": d.get("asname") or d.get("as"), "type": "hosting" if d.get("hosting") else "isp"},
                    "company": {"name": d.get("org") or d.get("isp"), "type": "hosting" if d.get("hosting") else "isp"},
                    "_src": "ip-api",
                }
        except Exception as e:  # noqa: BLE001
            log.debug("ip-api 失败: %s", e)

    return None


# ────────────────────────── 评分 ──────────────────────────
def score(info: dict[str, Any], node_name: str) -> dict[str, Any]:
    """0–100 的纯净度分，越高越干净。"""
    s = 100
    tags: list[str] = []
    if not isinstance(info, dict):
        info = {}

    # 缓存里的这三块可能被历史版本写成字符串 / null，直接 .get 会 AttributeError，
    # 而 score 是在渲染线程里跑的 —— 一次抛错整张节点表就白了
    loc = info.get("location") if isinstance(info.get("location"), dict) else {}
    asn = info.get("asn") if isinstance(info.get("asn"), dict) else {}
    comp = info.get("company") if isinstance(info.get("company"), dict) else {}
    cc = str(loc.get("country_code") or loc.get("country") or "").upper()
    expect = FLAG_CC.get(str(node_name or '')[:2], "")

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

    is_hosting = bool(
        info.get("is_datacenter")
        or str(comp.get("type", "")).lower() == "hosting"
        or str(asn.get("type", "")).lower() == "hosting"
    )
    if is_hosting:
        s -= 30
        # ipapi.is 的 datacenter 有时是对象、有时直接是机房名字符串
        dc_obj = info.get("datacenter")
        dc = (dc_obj.get("datacenter") or dc_obj.get("name") or "") if isinstance(dc_obj, dict) else str(dc_obj or "")
        tags.append(f"机房{('/' + dc) if dc else ''}")
    elif str(comp.get("type", "")).lower() == "isp" or info.get("is_mobile"):
        tags.append("住宅/ISP")
    elif comp.get("type"):
        tags.append(str(comp["type"]))

    mismatch = bool(expect and cc and expect != cc)
    if mismatch:
        s -= 18
        tags.append(f"落地≠标称({cc})")

    s = max(0, min(100, s))
    if s >= 85:
        grade, color = "极洁净", "ok"
    elif s >= 70:
        grade, color = "良好", "good"
    elif s >= 50:
        grade, color = "一般", "mid"
    else:
        grade, color = "较差", "bad"

    return {
        "score": s,
        "grade": grade,
        "color": color,
        "tags": tags[:4],
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
