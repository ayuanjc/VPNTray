"""订阅下载 / 解析 / 规范化。

支持 Clash / mihomo 兼容 YAML 订阅（本机订阅即为该格式）。
下载优先直连（订阅域名在规则里是 DIRECT），失败再借本机其它代理端口重试，
避免「自己代理自己」的死循环。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import socket
import time
from datetime import datetime
from typing import Any

import requests
import yaml

from .paths import SUB_CACHE, ensure_dirs

log = logging.getLogger("sub")

_LOCAL_PROXY_CANDIDATES = ("127.0.0.1:7891", "127.0.0.1:7897", "127.0.0.1:7890")


class SubscriptionError(RuntimeError):
    pass


# 机场常用「假节点」承载账号信息，不参与真实代理
INFO_MARKERS = (
    "剩余流量", "套餐到期", "到期时间", "流量重置", "距离下次",
    "官网", "群组", "订阅地址", "重置日", "有效期",
)


def is_info_node(name: str) -> bool:
    return any(m in name for m in INFO_MARKERS)


_UNIT_GB = {"B": 1 / 1024**3, "KB": 1 / 1024**2, "MB": 1 / 1024, "GB": 1, "TB": 1024, "PB": 1024**2}
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGT]B|[KMGT])", re.I)
_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
_FOREVER = ("长期", "永久", "不过期", "无限", "∞", "长期有效")


def parse_info(info: list[str] | None) -> dict[str, Any]:
    """把「剩余流量：121.45 GB」「套餐到期：2026-03-15」这类节点名解析成结构化信息。

    机场的花样很多（有的写「流量：100G/月 · 已用 12G」「距离下次重置还有 5 天」），
    这里只做宽松提取，拿不到就留空，绝不抛异常。
    """
    out: dict[str, Any] = {
        "traffic_left_gb": None,
        "traffic_total_gb": None,
        "traffic_text": "",
        "expire_text": "",
        "expire_ts": None,
        "days_left": None,
        "reset_text": "",
        "raw": list(info or []),
    }
    for raw in info or []:
        text = str(raw or "").strip()
        if not text:
            continue
        # 流量：抓「剩余/可用/流量」后面的第一条容量
        if any(k in text for k in ("剩余流量", "流量剩余", "剩余", "流量", "可用流量")):
            m = _NUM_RE.search(text)
            unit_gb = lambda u: _UNIT_GB.get(u.upper() if u.upper().endswith("B") else u.upper() + "B", 1)
            if m and out["traffic_left_gb"] is None and ("剩余" in text or "可用" in text):
                try:
                    out["traffic_left_gb"] = round(float(m.group(1)) * unit_gb(m.group(2)), 2)
                except (TypeError, ValueError):
                    pass
                out["traffic_text"] = text
            elif m and out["traffic_total_gb"] is None:
                try:
                    out["traffic_total_gb"] = round(float(m.group(1)) * unit_gb(m.group(2)), 2)
                except (TypeError, ValueError):
                    pass
                out["traffic_text"] = out["traffic_text"] or text
        # 到期
        if any(k in text for k in ("到期", "有效期", "过期")):
            out["expire_text"] = text
            if any(f in text for f in _FOREVER):
                out["expire_ts"] = 0          # 0 表示长期有效
                out["days_left"] = None
            else:
                m = _DATE_RE.search(text)
                if m:
                    try:
                        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                        out["expire_ts"] = int(dt.timestamp())
                        out["days_left"] = (dt.date() - datetime.now().date()).days
                    except ValueError:
                        pass
        # 重置日
        if any(k in text for k in ("重置", "距离下次")):
            out["reset_text"] = text
    return out


def _finite(s: Any, cap: float = 1e15) -> float:
    """订阅头里的数值：拒绝 NaN / ±Inf / 负数 / 超范围。"""
    try:
        v = float(s)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")) or v < 0 or v >= cap:
        return 0.0
    return v


def parse_userinfo(header: str | None) -> dict[str, Any]:
    """解析标准的 `subscription-userinfo: upload=1; download=2; total=3; expire=1700000000`。

    这个响应头几乎所有机场都带，拿它算余额比去翻假节点名准得多，
    而且只需读 header、不用下载整份配置。
    """
    out: dict[str, Any] = {}
    if not header:
        return out
    kv: dict[str, str] = {}
    for part in str(header).split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        kv[k.strip().lower()] = v.strip()
    total = _finite(kv.get("total"))
    used = _finite(kv.get("upload")) + _finite(kv.get("download"))
    if total > 0:
        out["traffic_total_gb"] = round(total / 1024 ** 3, 2)
        left = max(0.0, (total - used) / 1024 ** 3)
        out["traffic_left_gb"] = round(left, 2)
        out["traffic_text"] = f"剩余流量：{left:.2f} GB / 共 {out['traffic_total_gb']:.0f} GB"
    exp = int(_finite(kv.get("expire"), cap=4102444800))  # 2100-01-01 之前才算有效
    if exp > 0:
        try:
            dt = datetime.fromtimestamp(exp)
        except (OverflowError, OSError, ValueError):
            dt = None
        if dt is not None:
            out["expire_ts"] = exp
            out["expire_text"] = f"套餐到期：{dt:%Y-%m-%d}"
            out["days_left"] = (dt.date() - datetime.now().date()).days
    elif "expire" in kv and not (kv.get("expire") or "").strip().strip("0"):
        out["expire_ts"] = 0
        out["expire_text"] = "套餐到期：长期有效"
    return out


def fetch_userinfo(url: str, ua: str, local_port: int, timeout: int = 10) -> dict[str, Any]:
    """只取响应头里的流量/到期信息，不落盘、不解析配置。"""
    if not url:
        return {}
    for proxies in _proxies_candidates(local_port):
        try:
            r = requests.get(url, headers={"User-Agent": ua, "Accept": "*/*"},
                             proxies=proxies or None, timeout=timeout, stream=True)
            try:
                hdr = r.headers.get("subscription-userinfo") or r.headers.get("Subscription-Userinfo")
            finally:
                r.close()
            info = parse_userinfo(hdr)
            if info:
                return info
        except Exception as e:  # noqa: BLE001
            log.debug("取 %s 流量信息失败: %s", url[:36], e)
    return {}


_CAND_TTL = 30.0
_cand_cache: dict[str, Any] = {"ts": 0.0, "port": None, "out": None}


def _proxies_candidates(local_port: int) -> list[dict[str, str]]:
    """可用的代理列表，直连排第一。

    每个候选都要 socket 探测一次，全都不通时一轮就是 1.6 秒 —— 而刷新订阅
    信息会连续调用，全卡在 UI 线程上。所以：本机回环探测超时压到 0.15s，
    并且结果缓存 30 秒（端口开没开不会一秒一变）。
    """
    now = time.time()
    c = _cand_cache
    if c["out"] is not None and c["port"] == local_port and (now - c["ts"]) < _CAND_TTL:
        return [dict(d) for d in c["out"]]

    out: list[dict[str, str]] = [{}]
    seen: set[str] = set()
    for a in (f"127.0.0.1:{local_port}", *_LOCAL_PROXY_CANDIDATES):
        host, _, port = a.partition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=0.15):
                pass
        except (OSError, ValueError):
            continue
        url = f"http://{a}"
        if url in seen:
            continue
        seen.add(url)
        out.append({"http_proxy": url, "https_proxy": url})

    c.update(ts=now, port=local_port, out=[dict(d) for d in out])
    return out


def fetch(url: str, ua: str, local_port: int, timeout: int = 25) -> bytes:
    headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Cache-Control": "no-cache",
    }
    last: Exception | None = None
    for prox in _proxies_candidates(local_port):
        try:
            with requests.Session() as s:
                s.trust_env = False  # 不读环境变量里的系统代理
                s.proxies.update(prox)
                with s.get(url, headers=headers, timeout=timeout) as r:
                    if r.status_code != 200:
                        raise SubscriptionError(f"HTTP {r.status_code}")
                    content = r.content
            if len(content) < 64:
                raise SubscriptionError(f"内容过短: {len(content)} bytes")
            via = prox.get("http_proxy") or "direct"
            log.info("订阅下载成功 via %s, %d bytes", via, len(content))
            return content
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("订阅下载失败 via %s: %s", prox.get("http_proxy") or "direct", e)
    raise SubscriptionError(f"订阅下载失败: {last}")


def _maybe_base64(text: str) -> str:
    """有些机场给的是 base64 编码的节点列表。"""
    s = text.strip()
    if "proxies:" in s:
        return s
    compact = "".join(s.split())
    if len(compact) < 32 or len(compact) % 4:
        return s
    try:
        raw = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False)
        dec = raw.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return s
    return dec if ("proxies:" in dec or "://" in dec) else s


MAX_SUB_BYTES = 8 * 1024 * 1024   # 订阅体量上限，防止恶意/异常订阅吃爆内存
MAX_RULES = 20000


def parse(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_SUB_BYTES:
        raise SubscriptionError(f"订阅过大：{len(content) / 1024 / 1024:.1f} MB")
    text = content.decode("utf-8", "ignore")
    text = _maybe_base64(text)
    try:
        cfg = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as e:
        raise SubscriptionError(f"YAML 解析失败: {e}") from e
    if not isinstance(cfg, dict):
        raise SubscriptionError("订阅内容不是 YAML 字典")
    if not isinstance(cfg.get("proxies"), list) or not cfg["proxies"]:
        raise SubscriptionError("订阅里没有 proxies（可能是不支持的明文节点列表格式）")
    if not isinstance(cfg.get("proxy-groups"), list) or not cfg["proxy-groups"]:
        raise SubscriptionError("订阅里没有 proxy-groups")
    return cfg


def normalize(cfg: dict[str, Any]) -> dict[str, Any]:
    """只保留我们需要的部分，并做一致性修补。

    机场常把「剩余流量：xxx」这类假节点塞进 proxies 里用于展示，
    它们会被优先选中导致上不了网，这里统一剔除并单独返回。
    """
    raw_proxies: list[dict[str, Any]] = []
    for p in cfg.get("proxies") or []:
        if not isinstance(p, dict):
            continue                      # 畸形订阅里会混进字符串/数字
        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        raw_proxies.append(p)
    info = [p["name"] for p in raw_proxies if is_info_node(p["name"])]

    # 同名节点会让 mihomo 直接拒绝加载（proxy name duplicated），必须去重
    proxies: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for p in raw_proxies:
        n = p["name"]
        if is_info_node(n) or n in seen_names:
            continue
        if not all(k in p for k in ("type", "server", "port")):
            log.warning("丢弃字段不全的节点：%s", n)
            continue
        try:
            p = {**p, "port": int(p["port"])}
        except (TypeError, ValueError):
            log.warning("丢弃端口非法的节点：%s", n)
            continue
        if not (0 < p["port"] < 65536):
            continue
        seen_names.add(n)
        proxies.append(p)

    names = seen_names

    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    declared_group_names = {
        g["name"] for g in (cfg.get("proxy-groups") or [])
        if isinstance(g, dict) and isinstance(g.get("name"), str) and g["name"].strip()
    }
    for g in cfg.get("proxy-groups") or []:
        if not isinstance(g, dict):
            continue
        gname = g.get("name")
        if not isinstance(gname, str) or not gname.strip() or gname in seen_groups:
            continue
        seen_groups.add(gname)
        g = dict(g)
        members: list[str] = []
        for n in g.get("proxies") or []:
            if not isinstance(n, str):
                continue
            if n == gname or n in members:   # 组把自己列为成员 → mihomo 报 loop
                continue
            if n in names or n in declared_group_names or n in {"DIRECT", "REJECT", "PASS"}:
                members.append(n)
        g["proxies"] = members
        groups.append(g)
    if not groups:
        raise SubscriptionError("订阅里的策略组全部不可用")

    # 规则：strip 后过滤，限长限量；MATCH 由我们自己拼，订阅里的一律丢掉
    rules: list[str] = []
    for r in cfg.get("rules") or []:
        if not isinstance(r, str):
            continue
        r = r.strip()
        if not r or r.upper().startswith("MATCH"):
            continue
        rules.append(r[:512])
        if len(rules) >= MAX_RULES:
            log.warning("订阅规则超过 %d 条，已截断", MAX_RULES)
            break

    dns = cfg.get("dns") if isinstance(cfg.get("dns"), dict) else {}

    return {
        "proxies": proxies,
        "proxy-groups": groups,
        "rules": rules,
        "dns": dns,
        "info": info,
        "extra": {
            k: v
            for k, v in cfg.items()
            if k not in {"proxies", "proxy-groups", "rules", "dns"}
        },
    }


def save_cache(raw: bytes, content: dict[str, Any]) -> str:
    """原子写订阅缓存：崩在中间也不会留下半截文件。"""
    ensure_dirs()
    tmp = SUB_CACHE.with_name(SUB_CACHE.name + ".tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SUB_CACHE)
    except OSError as e:
        log.warning("写入订阅缓存失败: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return hashlib.sha256(raw).hexdigest()


def load_cache() -> dict[str, Any] | None:
    if not SUB_CACHE.exists():
        return None
    try:
        return parse(SUB_CACHE.read_bytes())
    except Exception as e:  # noqa: BLE001
        log.warning("缓存订阅解析失败: %s", e)
        return None
