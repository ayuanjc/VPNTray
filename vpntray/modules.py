"""场景 / 模块注册表：把「Steam 加速」这类东西从写死的代码变成数据。

为什么要有这一层
----------------
2.5.8 之前场景是 configgen.py 里的字面量 SCENES，想加一个「Epic 加速」必须改
代码再重新出包。但场景本质上是三样数据：一组域名、一个策略组、一份排序偏好，
完全没有逻辑，所以搬进 data/modules/<key>.json —— 面板、手改文件、AI 接口
改的是同一份东西。

一个模块 = 一个策略组 + 若干条分流规则
--------------------------------------
kind=scene   建策略组（select，可选带 fallback 自动子组），match 里的域名/
             规则全部指到这个组。
kind=rule    不建组，match 里的域名/规则直接指到 group.target
             （留空 = 主策略组，或 DIRECT / REJECT / 订阅里已有的组名），
             用来做「这个网站直连」「这个网段走指定线路」这类纯规则模块。

落盘约定
--------
* 一个模块一个文件，文件名就是 key，原子写（tmp + os.replace）。
* 单个文件坏了不影响其它模块：load_all() 跳过并记 WARNING。
* 内置模块只在第一次运行时物化（.seeded 标记），删掉就是删掉，不会被偷偷
  写回来；想恢复用 reset_builtins()（面板上的「恢复内置」）。
* 保存前一律过 validate()：策略组名重名、规则里写死目标组、域名带通配符
  这些都会让 mihomo 直接起不来，必须在落盘前挡住。
"""
from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import paths

log = logging.getLogger("modules")

SCHEMA = 1
MAX_MODULES = 64
MAX_DOMAINS = 400
MAX_RULES = 200

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)+$")

# key / 组名被内核和本程序自己占用，用户模块不能用
RESERVED_KEYS = {"ai", "direct", "reject", "global", "match", "main", "proxy"}
RESERVED_TITLES = {"DIRECT", "REJECT", "PASS", "GLOBAL", "PROXY", "MATCH"}

GROUP_TYPES = ("select", "fallback", "url-test", "load-balance")

# 只放行「分流」类规则：SCRIPT / SUB-RULE / RULE-SET 之外的东西（比如 EXTERNAL）
# 能执行代码或引用外部文件，不属于「用户自己填个域名」的范围。
RULE_TYPES = {
    "DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD", "DOMAIN-REGEX",
    "IP-CIDR", "IP-CIDR6", "IP-SUFFIX", "IP-ASN",
    "SRC-IP-CIDR", "SRC-PORT", "DST-PORT",
    "PROCESS-NAME", "PROCESS-PATH", "PROCESS-NAME-REGEX",
    "GEOSITE", "GEOIP", "NETWORK", "RULE-SET",
}

SEEDED = ".seeded"
_lock = threading.RLock()
_cache: dict[str, Any] = {"sig": None, "items": []}


# ────────────────────────── 基础工具 ──────────────────────────
def _int(v: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if lo <= n <= hi else default


def _uniq(conv, seq: Any, cap: int) -> list:
    """按顺序保序去重（去重看字符串形式，避免 "1" 和 1 各占一条）。"""
    out: list = []
    seen: set[str] = set()
    for x in seq if isinstance(seq, (list, tuple)) else []:
        try:
            v = conv(x)
        except Exception:  # noqa: BLE001
            continue
        if v is None:
            continue
        k = str(v)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(v)
        if len(out) >= cap:
            break
    return out


def _s(v: Any, cap: int = 200) -> str:
    return str(v if v is not None else "").strip()[:cap]


def _domain(v: Any) -> str | None:
    """域名后缀归一：去掉用户习惯写的 *.  /  +.  前缀和结尾的点。"""
    d = _s(v, 253).lower()
    while d.startswith("*.") or d.startswith("+.") or d.startswith("*"):
        d = d[1:] if d.startswith("*") else d[2:]
        d = d.lstrip(".")
    return d.strip(". ") or None


def dir_path() -> Path:
    return paths.MODULES


def _path(key: str) -> Path:
    return dir_path() / f"{key}.json"


def _sig() -> tuple:
    d = dir_path()
    try:
        return tuple(sorted(
            (p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in d.glob("*.json") if p.is_file()
        ))
    except OSError:
        return ()


# ────────────────────────── 归一 / 校验 ──────────────────────────
def normalize(raw: Any) -> dict[str, Any]:
    """把任意脏输入收敛成合法形状。不做合法性判断（那是 validate 的事）。"""
    r = raw if isinstance(raw, dict) else {}
    g = r.get("group") if isinstance(r.get("group"), dict) else {}
    m = r.get("match") if isinstance(r.get("match"), dict) else {}
    key = _s(r.get("key"), 32).lower()
    kind = "rule" if _s(r.get("kind"), 8).lower() == "rule" else "scene"
    return {
        "schema": SCHEMA,
        "key": key,
        "title": _s(r.get("title"), 32),
        "desc": _s(r.get("desc"), 80),
        "kind": kind,
        "enabled": bool(r.get("enabled", True)),
        "builtin": bool(r.get("builtin", False)),
        "icon": _s(r.get("icon"), 4) or ("🧩" if kind == "rule" else "🚀"),
        "order": _int(r.get("order"), 50, -999, 9999),
        "group": {
            "type": _s(g.get("type"), 16).lower() or "select",
            "auto": bool(g.get("auto", False)),
            "auto_title": _s(g.get("auto_title"), 32),
            "probe": _s(g.get("probe"), 300),
            "interval": _int(g.get("interval"), 180, 30, 3600),
            "timeout": _int(g.get("timeout"), 3000, 1000, 10000),
            "target": _s(g.get("target"), 64),
        },
        "match": {
            "domains": _uniq(_domain, m.get("domains"), MAX_DOMAINS),
            "rules": _uniq(lambda x: _s(x, 300), m.get("rules"), MAX_RULES),
            "cidrs": _uniq(lambda x: _s(x, 64), m.get("cidrs"), MAX_RULES),
        },
        "prefer_regions": _uniq(lambda x: _s(x, 16), r.get("prefer_regions"), 12),
        "pinned": _s(r.get("pinned"), 120),
    }


def validate(mod: dict[str, Any]) -> list[str]:
    """返回全部问题（不是第一个）：一次改好几处时用户不用来回试。"""
    errs: list[str] = []
    key = mod.get("key") or ""
    title = mod.get("title") or ""
    if not KEY_RE.match(key):
        errs.append("key 只能用小写字母/数字/-/_，2–32 位")
    elif key in RESERVED_KEYS:
        errs.append(f"key「{key}」是保留名，换一个")
    if not title:
        errs.append("组名不能为空")
    elif len(title) > 24:
        errs.append("组名最长 24 个字符")
    elif "," in title or "\n" in title:
        errs.append("组名里不能有逗号或换行")
    elif title.upper() in RESERVED_TITLES:
        errs.append(f"组名「{title}」是保留名，换一个")
    if mod.get("kind") not in ("scene", "rule"):
        errs.append("kind 只能是 scene 或 rule")

    g = mod.get("group") or {}
    if g.get("type") not in GROUP_TYPES:
        errs.append("组类型只能是 " + " / ".join(GROUP_TYPES))
    probe = g.get("probe") or ""
    if probe and not (probe.startswith(("http://", "https://")) and " " not in probe):
        errs.append("探测地址必须是 http(s) 链接且不含空格")
    if g.get("auto") and g.get("type") != "select":
        errs.append("只有 select 组能带「自动」子组")

    m = mod.get("match") or {}
    for d in m.get("domains") or []:
        if not DOMAIN_RE.match(d):
            errs.append(f"域名不合法：{d}（写后缀即可，不用 *. 前缀）")
            break
    for c in m.get("cidrs") or []:
        try:
            ipaddress.ip_network(str(c), strict=False)
        except ValueError:
            errs.append(f"网段不合法：{c}")
            break
    for r in m.get("rules") or []:
        head, sep, tail = str(r).partition(",")
        if not sep:
            errs.append(f"规则「{r}」缺少值，格式是 类型,值")
            break
        if head.strip().upper() not in RULE_TYPES:
            errs.append(f"规则类型 {head.strip()} 不支持")
            break
        if "," in tail and head.strip().upper() != "DOMAIN-REGEX":
            errs.append(f"规则「{r}」不要写目标组，目标由程序按模块自动拼")
            break
    if not (m.get("domains") or m.get("rules") or m.get("cidrs")):
        errs.append("至少填一个域名 / 规则 / 网段")
    return errs


# ────────────────────────── 读写 ──────────────────────────
def _read_file(p: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("模块文件 %s 读取失败（已跳过）：%s", p.name, e)
        return None
    mod = normalize(raw)
    if not mod["key"]:
        mod["key"] = p.stem
    return mod


def _write_file(mod: dict[str, Any]) -> None:
    d = dir_path()
    d.mkdir(parents=True, exist_ok=True)
    p = _path(mod["key"])
    tmp = p.with_name(p.name + ".tmp")
    text = json.dumps(mod, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def load_all(force: bool = False) -> list[dict[str, Any]]:
    """全部模块，按 (order, 组名) 排序。带 mtime 缓存：面板 1.8s 轮询不重读磁盘。"""
    with _lock:
        s = _sig()
        if force or _cache["sig"] != s:
            items: list[dict[str, Any]] = []
            for p in sorted(dir_path().glob("*.json")):
                mod = _read_file(p)
                if mod:
                    items.append(mod)
            items.sort(key=lambda x: (int(x.get("order") or 0), x.get("title") or ""))
            _cache["sig"] = s
            _cache["items"] = items
        return copy.deepcopy(_cache["items"])


def load(key: str) -> dict[str, Any] | None:
    key = _s(key, 32).lower()
    for m in load_all():
        if m["key"] == key:
            return m
    return None


def save(raw: Any) -> tuple[bool, str]:
    """新建或覆盖一个模块。key 不可变（要改名就删了重建）。"""
    mod = normalize(raw)
    errs = validate(mod)
    if errs:
        return False, "；".join(errs[:4])
    with _lock:
        items = load_all(force=True)
        for other in items:
            if other["key"] == mod["key"]:
                continue
            if (other["title"] or "").lower() == mod["title"].lower():
                return False, f"组名「{mod['title']}」已被模块 {other['key']} 使用"
        olds = {m["key"] for m in items}
        if mod["key"] not in olds and len(items) >= MAX_MODULES:
            return False, f"模块数量上限 {MAX_MODULES} 个"
        old = next((m for m in items if m["key"] == mod["key"]), None)
        if old:
            mod["builtin"] = bool(old.get("builtin"))
        try:
            _write_file(mod)
        except OSError as e:
            return False, f"写入失败：{e}"
        _cache["sig"] = None
    log.info("模块已保存 %s（%s，%d 条域名）", mod["key"], mod["title"],
             len(mod["match"]["domains"]))
    return True, ""


def update(key: str, **fields: Any) -> tuple[bool, str]:
    """改单个字段（钉节点 / 开关），保留文件里其它内容。"""
    mod = load(key)
    if not mod:
        return False, f"未知模块：{key}"
    mod.update(fields)
    return save(mod)


def remove(key: str) -> tuple[bool, str]:
    key = _s(key, 32).lower()
    if not KEY_RE.match(key):
        return False, "模块 key 不合法"
    p = _path(key)
    if not p.exists():
        return False, f"模块不存在：{key}"
    try:
        p.unlink()
    except OSError as e:
        return False, f"删除失败：{e}"
    with _lock:
        _cache["sig"] = None
    log.info("模块已删除 %s", key)
    return True, ""


def export_all() -> dict[str, Any]:
    return {"schema": SCHEMA, "exported": int(time.time()), "modules": load_all()}


def import_all(payload: Any) -> tuple[int, list[str]]:
    """导入一组模块。逐个校验，坏的单独报错，好的照常落盘。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return 0, ["JSON 解析失败"]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("modules") if isinstance(payload.get("modules"), list) else [payload]
    else:
        return 0, ["不认识的结构，需要 {modules: [...]} 或单个模块对象"]
    ok = 0
    errs: list[str] = []
    for it in items:
        good, err = save(it)
        if good:
            ok += 1
        else:
            errs.append(f"{_s((it or {}).get('key'), 32) or '?'}: {err}")
    return ok, errs


# ────────────────────────── 内置模块 ──────────────────────────
def builtins() -> list[dict[str, Any]]:
    """出厂自带的两个场景。形状和用户自己加的完全一样，没有任何特权。"""
    return [
        normalize({
            "key": "steam",
            "title": "Steam 加速",
            "desc": "商店 / 社区 / 下载 CDN",
            "kind": "scene",
            "builtin": True,
            "icon": "🎮",
            "order": 10,
            "group": {"type": "select", "probe": "https://store.steampowered.com/robots.txt"},
            "prefer_regions": ["香港", "台湾", "日本", "韩国", "新加坡"],
            "match": {"domains": [
                "steampowered.com",      # 商店 / 登录 / 支付
                "steamcommunity.com",    # 社区、创意工坊、市场
                "steamstatic.com",       # 图片与静态资源
                "steamcontent.com",      # 下载 CDN（游戏本体）
                "steamusercontent.com",  # 工坊内容
                "steamgames.com",
                "steamserver.net",
                "steam-chat.com",
                "steamconnecttest.com",
                "steamdeck.com",
                "s.team",                # 短链与登录跳转
            ]},
        }),
        normalize({
            "key": "github",
            "title": "GitHub 加速",
            "desc": "git / Release / Pages / 容器镜像",
            "kind": "scene",
            "builtin": True,
            "icon": "🐙",
            "order": 20,
            # 自动子组：这台机器 100 个节点里 88 个是死的，钉死一个出口就会
            # 整个 GitHub 打不开，所以默认带一个 fallback 子组自己挑活的。
            "group": {
                "type": "select",
                "auto": True,
                "auto_title": "GitHub 自动",
                "probe": "https://github.com/robots.txt",
                "interval": 180,
                "timeout": 3000,
            },
            "prefer_regions": ["香港", "日本", "新加坡", "台湾", "美国"],
            "match": {
                "domains": [
                    "github.com",            # 含 api / gist / codeload / uploads / npm.pkg
                    "githubusercontent.com",  # raw / objects / media(LFS) / release-assets
                    "githubassets.com",      # 页面静态资源
                    "github.io",             # Pages
                    "ghcr.io",               # 容器镜像
                    "github.dev",            # 网页编辑器
                    "githubcopilot.com",
                    "github.blog",
                    "githubstatus.com",      # 状态页
                ],
                # Release 老式直链会 302 到 S3，那些域名不属于上面任何一个后缀，
                # 掉到 MATCH 就按 Netflix 的落地点绕地球一圈，下载掉到几十 KB/s。
                "rules": [
                    "DOMAIN-KEYWORD,github-production-release-asset",
                    "DOMAIN,github-cloud.s3.amazonaws.com",
                ],
            },
        }),
    ]


def ensure_builtins(settings: Any = None) -> int:
    """首次运行物化内置模块；带上 settings 里 2.5.8 时代的开关/钉住/偏好。

    返回新建的个数。已经物化过（.seeded 存在）就什么也不做 —— 用户删掉的
    内置模块不会在下次启动时复活。
    """
    d = dir_path()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("模块目录建不出来 %s: %s", d, e)
        return 0
    seeded = d / SEEDED
    if seeded.exists():
        return 0
    made = 0
    legacy = {
        "steam": ("steam_enabled", "steam_node", "steam_prefer_regions"),
        "github": ("github_enabled", "github_node", "github_prefer_regions"),
    }
    for mod in builtins():
        k = mod["key"]
        if not _path(k).exists():
            if settings is not None:
                ek, nk, pk = legacy.get(k, ("", "", ""))
                # Settings 没有 __contains__：x in settings 会退化成 __getitem__(0)
                # 的迭代协议，直接 KeyError(0)。只能用 get 判空。
                if ek and settings.get(ek) is not None:
                    mod["enabled"] = bool(settings.get(ek))
                if nk:
                    mod["pinned"] = str(settings.get(nk) or "")
                pref = settings.get(pk) if pk else None
                if isinstance(pref, list) and pref:
                    mod["prefer_regions"] = [str(x) for x in pref][:12]
            try:
                _write_file(mod)
                made += 1
            except OSError as e:
                log.warning("写内置模块 %s 失败: %s", k, e)
    try:
        seeded.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    if made:
        log.info("已物化 %d 个内置模块到 %s", made, d)
    return made


def reset_builtins() -> int:
    """把内置模块写回出厂值（用户自建模块不动）。"""
    n = 0
    for mod in builtins():
        try:
            _write_file(mod)
            n += 1
        except OSError as e:
            log.warning("恢复内置模块 %s 失败: %s", mod["key"], e)
    with _lock:
        _cache["sig"] = None
    try:
        (dir_path() / SEEDED).write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    return n


def summary() -> list[dict[str, Any]]:
    """给面板/AI 用的轻量列表（不带完整域名表）。"""
    out = []
    for m in load_all():
        mt = m.get("match") or {}
        out.append({
            "key": m["key"],
            "title": m["title"],
            "desc": m.get("desc", ""),
            "kind": m["kind"],
            "enabled": bool(m.get("enabled", True)),
            "builtin": bool(m.get("builtin")),
            "icon": m.get("icon", ""),
            "order": int(m.get("order") or 0),
            "domains": len(mt.get("domains") or []),
            "rules": len(mt.get("rules") or []),
            "cidrs": len(mt.get("cidrs") or []),
            "auto": bool((m.get("group") or {}).get("auto")),
            "target": str((m.get("group") or {}).get("target") or ""),
            "prefer": list(m.get("prefer_regions") or []),
            "pinned": m.get("pinned", ""),
        })
    return out
