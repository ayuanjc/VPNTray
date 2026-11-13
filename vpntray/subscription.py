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
import tempfile
import threading
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
            # 只读响应头，不需要给它长超时；候选逐个试，单个卡住会把 UI 线程拖死
            r = requests.get(url, headers={"User-Agent": ua, "Accept": "*/*"},
                             proxies=proxies or None,
                             timeout=min(_MAX_NET_TIMEOUT, timeout), stream=True)
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
                # 单次读写上限 15 秒：total 传进来的 25 秒是「整包」预算，但 requests
                # 的 timeout 是单次 socket 操作，放大到 25 秒在断流节点上等于白等。
                with s.get(url, headers=headers,
                           timeout=min(_MAX_NET_TIMEOUT, timeout), stream=True) as r:
                    if r.status_code != 200:
                        raise SubscriptionError(f"HTTP {r.status_code}")
                    # 以前只在 parse() 里检查上限，但那时整包已经读进内存了：
                    # 一个 800MB 的订阅能先把机器拖死。边读边卡。
                    limit = MAX_SUB_BYTES
                    declared = r.headers.get("Content-Length") or ""
                    if declared.isdigit() and int(declared) > limit:
                        raise SubscriptionError(
                            f"订阅过大：{int(declared) / 1024 / 1024:.1f} MB")
                    buf = bytearray()
                    for chunk in r.iter_content(65536):
                        if not chunk:
                            continue
                        buf += chunk
                        if len(buf) > limit:
                            raise SubscriptionError(
                                f"订阅过大：超过 {limit // 1024 // 1024} MB")
                    content = bytes(buf)
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
    """有些机场给的是 base64 编码的节点列表。

    base64 至少四种变体：标准/URL-safe、带/不带 = padding。以前只认「长度能被 4
    整除」的标准串，URL-safe（- _ 代替 + /）和去掉 padding 的会被原样丢给 YAML，
    于是用户只看到「订阅内容不是 YAML 字典」。这里先归一化再解。
    """
    s = text.strip()
    if "proxies:" in s:
        return s
    compact = "".join(s.split())
    if len(compact) < 32:
        return s
    compact = compact.replace("-", "+").replace("_", "/").rstrip("=")
    try:
        raw = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=False)
    except Exception:  # noqa: BLE001
        return s
    if not raw:
        return s
    # 解出来必须是文本；图片/加密串解出来是乱码，直接当「不是 base64」放回去
    try:
        dec = raw.decode("utf-8")
    except UnicodeDecodeError:
        dec = raw.decode("gb18030", "ignore")
    return dec if ("proxies:" in dec or "://" in dec) else s


MAX_SUB_BYTES = 8 * 1024 * 1024   # 订阅体量上限，防止恶意/异常订阅吃爆内存
_MAX_NET_TIMEOUT = 15.0           # 单次网络操作的上限，断流节点不能把刷新挂住
MAX_RULES = 20000


def _decode_sub(content: bytes) -> str:
    """按 BOM → utf-8 → gb18030 的顺序解码订阅正文。

    以前一律 utf-8 errors="ignore"：GBK 订阅里每个中文字符都会变成 U+FFFD，
    节点名被污染后去重、规则匹配全部错位，用户看到的是「配置全是问号」。
    """
    if content.startswith(b"\xef\xbb\xbf"):
        return content[3:].decode("utf-8", "ignore")
    for enc in ("utf-8", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", "ignore")


def parse(content: bytes) -> dict[str, Any]:
    if len(content) > MAX_SUB_BYTES:
        raise SubscriptionError(f"订阅过大：{len(content) / 1024 / 1024:.1f} MB")
    text = _maybe_base64(_decode_sub(content))
    if len(text) > MAX_SUB_BYTES:
        # base64 解出来的可能比原始体还大，解码后再卡一次
        raise SubscriptionError(f"订阅过大：解码后 {len(text) / 1024 / 1024:.1f} MB")
    try:
        cfg = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError) as e:
        raise SubscriptionError(f"YAML 解析失败: {e}") from e
    except MemoryError as e:
        # 别名炸弹（&a [*a,*a,...]）展开是纯内存爆炸，safe_load 挡不住，只能兜住异常
        raise SubscriptionError("订阅内容疑似 YAML 别名炸弹，已拒绝解析") from e
    if not isinstance(cfg, dict):
        raise SubscriptionError("订阅内容不是 YAML 字典")
    # proxies 允许为空：proxy-providers 形态的订阅本来就没有内联节点，
    # 「有没有节点」交给 normalize 判断（那里能给出更准确的「没有可用节点」）。
    if cfg.get("proxies") is None:
        cfg["proxies"] = []
    if not isinstance(cfg.get("proxies"), list):
        raise SubscriptionError("订阅的 proxies 不是列表")
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

    specials = {"DIRECT", "REJECT", "PASS", "GLOBAL"}
    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for g in cfg.get("proxy-groups") or []:
        if not isinstance(g, dict):
            continue
        gname = g.get("name")
        if not isinstance(gname, str) or not gname.strip() or gname in seen_groups:
            continue
        seen_groups.add(gname)
        candidates.append(dict(g))

    # 反复剔除「成员全被过滤掉」的组：空 proxies 的组 mihomo 直接拒绝加载，
    # 一个空组就能让整份配置起不来。多轮是因为剔除某个组之后，原本引用它的
    # 兄弟组可能跟着变空（循环引用链），一轮清不干净。
    while True:
        alive = {g["name"] for g in candidates}
        kept: list[dict[str, Any]] = []
        for g in candidates:
            gname = g["name"]
            members: list[str] = []
            for n in g.get("proxies") or []:
                if not isinstance(n, str):
                    continue
                if n == gname or n in members:   # 组把自己列为成员 → mihomo 报 loop
                    continue
                if n in names or n in alive or n in specials:
                    members.append(n)
            g["proxies"] = members
            if members:
                kept.append(g)
            else:
                log.warning("丢弃没有可用成员的策略组：%s", gname)
        if len(kept) == len(candidates):
            break
        candidates = kept
    groups = candidates
    if not groups:
        raise SubscriptionError("订阅里的策略组全部不可用（成员都被过滤掉了）")

    # 规则：strip 后过滤，限长限量；MATCH 由我们自己拼，订阅里的一律丢掉
    valid_policy = {g["name"] for g in groups} | names | specials
    main_group = groups[0]["name"]
    rules: list[str] = []
    fixed_policy = 0
    for r in cfg.get("rules") or []:
        if not isinstance(r, str):
            continue
        r = r.strip()
        if not r or r.upper().startswith("MATCH"):
            continue
        parts = r.split(",")
        if len(parts) >= 2:
            policy = parts[-1].strip()
            if policy and policy not in valid_policy:
                # 悬空策略组名：规则指向一个不存在的组，mihomo 会整份拒绝加载；
                # 丢规则又会让流量漏走，所以改指主组（用户仍可在界面上切换）。
                parts[-1] = main_group
                r = ",".join(parts)
                fixed_policy += 1
        rules.append(r[:512])
        if len(rules) >= MAX_RULES:
            log.warning("订阅规则超过 %d 条，已截断", MAX_RULES)
            break
    if fixed_policy:
        log.warning("修正 %d 条指向不存在策略组的规则，已改指主组 %s",
                    fixed_policy, main_group)

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


_cache_lock = threading.Lock()   # 进程内串行化：两个刷新线程同时写会踩同一个临时文件


def save_cache(raw: bytes, content: dict[str, Any]) -> str:
    """原子写订阅缓存，返回原始内容的 sha256。

    以前写失败只 log 一行就照样返回哈希，上层把哈希当成「缓存已更新」，
    于是下次启动读到的是旧缓存、界面显示的却是新订阅 —— 静默不一致。
    现在写失败直接抛，调用方必须知道缓存没写成。
    """
    ensure_dirs()
    digest = hashlib.sha256(raw).hexdigest()
    with _cache_lock:
        fd = -1
        tmp = ""
        try:
            # mkstemp 拿唯一临时名 + 0600 权限；固定名 .tmp 在多实例下会互相覆盖
            fd, tmp = tempfile.mkstemp(prefix=SUB_CACHE.name + ".", suffix=".tmp",
                                       dir=str(SUB_CACHE.parent))
            with os.fdopen(fd, "wb") as f:
                fd = -1            # 已交给文件对象，别再手动 close
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SUB_CACHE)
            tmp = ""
        except OSError as e:
            raise SubscriptionError(f"写入订阅缓存失败：{e}") from e
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    return digest


def load_cache() -> dict[str, Any] | None:
    if not SUB_CACHE.exists():
        return None
    try:
        return parse(SUB_CACHE.read_bytes())
    except Exception as e:  # noqa: BLE001
        log.warning("缓存订阅解析失败: %s", e)
        return None
