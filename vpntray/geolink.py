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
# 失败也进缓存，但只放 60 秒：ip-api 免费额度是 45 次/分钟，
# 连点几次拓扑、或者在没网的时候反复打开，会被自己打满
FAIL_TTL = 60
_CACHE: dict[str, tuple[float, dict[str, Any] | None, float]] = {}
_LOCK = threading.Lock()

_FIELDS = "status,country,countryCode,city,lat,lon,query,isp"


def _http_json(url: str, timeout: float = 6.0) -> dict[str, Any] | None:
    try:
        # 用 with 关掉连接：拓扑是 40 秒一轮的轮询，Session 不关会一直涨句柄
        with requests.Session() as s:
            s.trust_env = False    # 关键：不读系统代理，否则查到的「本机 IP」就是出口 IP
            s.headers["User-Agent"] = "VPNTray/1.0"
            with s.get(url, timeout=timeout) as r:
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
        log.debug("geo http fail %s: %s", url, e)
    return None


def locate(query: str = "") -> dict[str, Any] | None:
    """query 为空 = 查本机公网 IP；也可以是 IP 或域名（ip-api 自己会解析域名）。"""
    key = (query or "self").lower()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < hit[2]:
        return hit[1]

    url = f"http://ip-api.com/json/{quote(str(query or ''), safe='')}?fields={_FIELDS}"
    d = _http_json(url)
    info = None
    if d and d.get("status") == "success":
        cand = {
            "ip": d.get("query") or query,
            "city": d.get("city") or "",
            "country": d.get("country") or "",
            "cc": str(d.get("countryCode") or "").upper(),
            "lat": d.get("lat"),
            "lon": d.get("lon"),
            "isp": d.get("isp") or "",
        }
        if cand["lat"] is not None and cand["lon"] is not None:
            info = cand
    with _LOCK:
        _CACHE[key] = (now, info, TTL if info else FAIL_TTL)
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
