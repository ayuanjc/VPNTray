"""链路拓扑：本机 → 代理入口 → 落地出口，三个点的地理定位。

取点方式：
  · 用户端 = 本机直连查到的公网 IP（trust_env=False，绕开系统代理，才是真实的家宽出口）
  · 代理入口 = 订阅里当前节点配置的 server 域名/IP（机场那台入口机）
  · 落地出口 = 通过代理查到的 exit IP（真正对目标网站现身的地址）

查询走 ip-api.com 免费接口（http、45 次/分钟），带 15 分钟缓存。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

from . import subscription

log = logging.getLogger("geolink")

TTL = 900
# 保留常量供外部引用；失败结果不再进缓存，原因见 locate()
FAIL_TTL = 60
_CACHE: dict[str, tuple[float, dict[str, Any] | None, float]] = {}
_LOCK = threading.Lock()

_UA = "VPNTray/1.0"
_TIME_OUT = 8.0
_FIELDS = "status,country,countryCode,city,lat,lon,query,isp"


def _http_json(url: str, timeout: float = _TIME_OUT,
               retries: int = 2) -> dict[str, Any] | None:
    """取一个 JSON 对象；网络错误重试（≤2 次）并指数退避。"""
    delay = 0.6
    for attempt in range(retries + 1):
        try:
            # 用 with 关掉连接：拓扑是 40 秒一轮的轮询，Session 不关会一直涨句柄
            with requests.Session() as s:
                s.trust_env = False    # 关键：不读系统代理，否则查到的「本机 IP」就是出口 IP
                s.headers["User-Agent"] = _UA
                with s.get(url, timeout=min(15.0, timeout)) as r:
                    if r.status_code == 429:
                        # 限流：再重试只会把额度烧得更久
                        log.debug("geo http %s -> 429", url)
                        return None
                    if r.status_code != 200:
                        log.debug("geo http %s -> %s", url, r.status_code)
                        return None
                    try:
                        data = r.json()
                    except ValueError:
                        log.debug("geo http %s -> 非 JSON 响应", url)
                        return None
                    return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            log.debug("geo http fail %s (%d): %s", url, attempt, e)
            if attempt >= retries:
                break
            time.sleep(delay)
            delay *= 2
    return None


def _cand(ip, city, country, cc, lat, lon, isp) -> dict[str, Any] | None:
    """把各家的字段裁成同一形状；缺经纬度就当没查到。"""
    if lat is None or lon is None:
        return None
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return {"ip": ip or "", "city": city or "", "country": country or "",
            "cc": str(cc or "").upper(), "lat": flat, "lon": flon,
            "isp": isp or ""}


def _geo(query: str) -> dict[str, Any] | None:
    """按「https 优先」的顺序问多家，任一家给出完整坐标就用它。"""
    q = quote(str(query or ""), safe="")
    # ip-api 免费版只走 http，明文响应在运营商侧可以被改写成任意假坐标，
    # 拓扑图上就会显示一个错误的国家而用户毫无察觉 —— 所以 https 服务排前面。
    d = _http_json(f"https://ipapi.co/{q}/json/")
    if isinstance(d, dict) and not d.get("error"):
        c = _cand(d.get("ip") or query, d.get("city"), d.get("country_name"),
                 d.get("country_code"), d.get("latitude"), d.get("longitude"),
                 d.get("org"))
        if c:
            return c
    d = _http_json(f"https://api.ip.sb/geoip/{q}")
    if isinstance(d, dict):
        c = _cand(d.get("ip") or query, d.get("city"), d.get("country"),
                 d.get("country_code"), d.get("latitude"), d.get("longitude"),
                 d.get("isp") or d.get("organization"))
        if c:
            return c
    for scheme in ("https", "http"):
        d = _http_json(f"{scheme}://ip-api.com/json/{q}?fields={_FIELDS}")
        if d and d.get("status") == "success":
            c = _cand(d.get("query") or query, d.get("city"), d.get("country"),
                     d.get("countryCode"), d.get("lat"), d.get("lon"),
                     d.get("isp"))
            if c:
                return c
    return None


def locate(query: str = "") -> dict[str, Any] | None:
    """query 为空 = 查本机公网 IP；也可以是 IP 或域名（服务商自己会解析域名）。"""
    key = (query or "self").lower()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and hit[1] is not None and now - hit[0] < hit[2]:
        return hit[1]

    info = _geo(query)
    with _LOCK:
        # 失败不进缓存：缓存空结果会让「刚连上网就点拓扑」在 60 秒里一直显示空，
        # 下一次真的查到结果也要等过期才见效。
        if info:
            _CACHE[key] = (now, info, TTL)
        else:
            _CACHE.pop(key, None)
    return info


def own_ip() -> dict[str, Any] | None:
    """本机公网出口（直连查，用于「用户端」那个点）。"""
    return locate("")


def server_of(node_name: str) -> str:
    """从订阅缓存里取当前节点的 server 地址。"""
    name = (node_name or "").strip()
    if not name:
        return ""
    try:
        cache = subscription.load_cache() or {}
        for p in cache.get("proxies") or []:
            if p.get("name") == name:
                return str(p.get("server") or "")
    except Exception as e:  # noqa: BLE001
        log.debug("读订阅缓存失败: %s", e)
    return ""


def entry(node_name: str) -> dict[str, Any] | None:
    """代理入口机的位置。"""
    host = server_of(node_name)
    if not host:
        return None
    info = locate(host)
    if info:
        info["host"] = host
    return info


def exit_point(exit_ip: str) -> dict[str, Any] | None:
    """落地出口的位置。"""
    ip = (exit_ip or "").strip()
    if not ip:
        return None
    return locate(ip)


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
