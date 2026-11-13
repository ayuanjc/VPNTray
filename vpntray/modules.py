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
# 已经「提案」过的内置模块 key 清单。老机器升级时新增的出厂模块靠它补种，
# 又不会把用户删掉的模块复活（删掉了 key 仍在清单里）。
SEEDED_KEYS = ".seeded-keys"
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


def _reserved_set(reserved: Iterable[str] | None) -> set[str]:
    """订阅里的组名集合归一成小写，供组名查重使用。

    为什么在做校验这一层：mihomo 的 proxy-groups 一旦出现同名组会直接起不
    来（整份配置被拒），而用户只是想给新场景起个「香港」这样的名字。把这个
    判断放到 save/validate 里，面板、AI 接口、手改文件走的是同一条路。
    """
    out: set[str] = set()
    for n in reserved or ():
        s = _s(n, 64)
        if s:
            out.add(s.lower())
    return out


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


def validate(mod: dict[str, Any], reserved: Iterable[str] | None = None) -> list[str]:
    """返回全部问题（不是第一个）：一次改好几处时用户不用来回试。

    reserved 是当前订阅里的组名/节点名集合（可选，旧调用方不传也能用）。
    """
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
    elif title.lower() in _reserved_set(reserved):
        # 和订阅里的策略组同名 → mihomo 报 duplicate group，整个配置都起不来。
        # 在这里挡住，比等内核 restart 失败再回滚好排查。
        errs.append(f"组名与订阅里的组重名：{title}")
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
            if head.strip().upper() == "DOMAIN-REGEX":
                # 只有类型没有正则时要说清缺的是什么：否则「格式是 类型,值」会
                # 让人以为正则后面还得补个目标组（那正好会把规则写错）
                errs.append(f"规则「{r}」缺正则表达式")
            else:
                errs.append(f"规则「{r}」缺少值，格式是 类型,值")
            break
        rtype = head.strip().upper()
        if rtype not in RULE_TYPES:
            errs.append(f"规则类型 {head.strip()} 不支持")
            break
        if rtype == "DOMAIN-REGEX":
            # 正则的正文本体不允许逗号和空白：configgen 用 f"{规则},{组名}" 拼装，
            # mihomo 又按逗号切参数，正则里一个 {1,3} 就能把这行规则切错位。
            if not tail.strip():
                errs.append(f"规则「{r}」缺正则表达式")
                break
            if "," in tail or any(ch.isspace() for ch in tail):
                errs.append(f"正则里不能有逗号或空白：{r}")
                break
            continue
        if "," in tail:
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


def save(raw: Any, reserved: Iterable[str] | None = None) -> tuple[bool, str]:
    """新建或覆盖一个模块。key 不可变（要改名就删了重建）。

    reserved: 订阅里已有的组名集合，用于组名查重（可选，向后兼容）。
    """
    mod = normalize(raw)
    errs = validate(mod, reserved)
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
        normalize({
            "key": "ocr-router",
            "title": "OCR 桥 / 阿里云直连",
            "desc": "图片先送到本机 8791 换成文字；dashscope 等阿里云 AI 端点直连",
            "kind": "rule",
            "builtin": True,
            "icon": "🔍",
            "order": 60,
            # 只插规则不建组：这条链路的重点是「别被代理绕出去」。
            # 阿里云在国内，走代理反而是绕地球一圈，还容易被风控。
            "group": {
                "type": "select",
                "target": "DIRECT",
                "probe": "https://dashscope.aliyuncs.com/compatible-mode/v1/models",
            },
            "match": {"domains": [
                "dashscope.aliyuncs.com",  # 兼容模式（OpenAI 风格）端点
                "aliyuncs.com",            # 各类阿里云服务
                "aliyun.com",
                "aliyuncs.com.cn",
                "aliyun-inc.com",
            ]},
        }),
    ] + _launcher_builtins()


# 各游戏启动器 / 平台的出厂场景。数据放在这里而不是写死在 builtins() 里，
# 是因为 2026 年这批启动器的域名还会变（Epic 换 CDN、EA 从 Origin 迁到 EA app），
# 追加一项只改这个元组，不用动 builtins() 的结构。
# 字段顺序：key, 标题, 图标, 排序, 说明, 探测 URL, 偏好地区, 域名, 规则
_LAUNCHERS: tuple[tuple, ...] = (
    (
        "xbox", "Xbox 加速", "🎮", 30, "Xbox 主机 / Xbox App / 微软商店 / 账号登录",
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        ["香港", "日本", "新加坡", "台湾", "美国"],
        [
            "xboxlive.com",          # 认证、好友、云存档、assets*.xboxlive.com
            "xboxab.com",            # Xbox Live 实验域
            "xbox.com",              # 官网 / 商店页
            "xboxservices.com",
            "xboxcdn.net",           # 封面与截图 CDN
            "login.live.com",        # 微软账号登录
            "account.live.com",
            "account.microsoft.com",
            "login.microsoftonline.com",
            "microsoftonline.com",
            "mp.microsoft.com",      # 商店后端（displaycatalog / purchase / licenses）
            "store-images.s-microsoft.com",
            "img-prod-cms-rt-microsoft-com.akamaized.net",
            "storeedgefd.dsx.mp.microsoft.com",
            "displaycatalog.mp.microsoft.com",
            "purchase.mp.microsoft.com",
            "licenses.mp.microsoft.com",
            "collections.mp.microsoft.com",
            "dl.delivery.mp.microsoft.com",      # 游戏本体下载
            "tlu.dl.delivery.mp.microsoft.com",
        ],
        ["DOMAIN-KEYWORD,xboxlive", "DOMAIN-KEYWORD,xboxab", "DOMAIN-KEYWORD,xboxservices"],
    ),
    (
        "epic", "Epic 加速", "🕹", 40, "Epic 商店 / 虚幻引擎 / EAC 反作弊",
        "https://www.epicgames.com/robots.txt",
        ["香港", "日本", "新加坡", "台湾", "韩国"],
        [
            "epicgames.com",
            "epicgamescdn.com",      # 游戏下载 CDN
            "epicgames.dev",
            "unrealengine.com",
            "unrealenginecdn.com",
            "fortnite.com",
            "easyanticheat.net",     # EAC 反作弊握手
            "epicgames-download1.akamaized.net",
        ],
        ["DOMAIN-KEYWORD,epicgames", "DOMAIN-KEYWORD,easyanticheat"],
    ),
    (
        "battlenet", "暴雪战网加速", "❄️", 50, "Battle.net / 魔兽 / 守望 / 暗黑",
        "https://www.blizzard.com/robots.txt",
        ["香港", "台湾", "日本", "韩国", "新加坡"],
        [
            "battle.net",
            "blizzard.com",
            "blizzard.cn",
            "battlenet.com.cn",
            "blzstatic.cn",          # 国服静态资源
            "blizzard.vo.llnwd.net",
            "blzddist1-a.akamaihd.net",
            "bnetproduct-a.akamaihd.net",
            "blizzard.akamaized.net",
        ],
        ["DOMAIN-KEYWORD,battle.net", "DOMAIN-KEYWORD,blizzard"],
    ),
    (
        "ea", "EA 加速", "🎯", 60, "EA app / Origin / EA 反作弊",
        "https://www.ea.com/robots.txt",
        ["香港", "日本", "新加坡", "台湾", "美国"],
        [
            "ea.com",
            "origin.com",
            "ea-play.com",
            "eadescs.com",
            "eaassets-a.akamaihd.net",
            "eac-cdn.ea.com",        # 反作弊组件
            "cloudsync.ea.com",
            "origin-a.akamaihd.net",
        ],
        ["DOMAIN-KEYWORD,eaassets", "DOMAIN-KEYWORD,eac-cdn"],
    ),
    (
        "ubisoft", "育碧加速", "🌀", 70, "Ubisoft Connect / 彩虹六号 / 刺客信条",
        "https://www.ubisoft.com/robots.txt",
        ["香港", "日本", "新加坡", "台湾", "欧洲"],
        [
            "ubisoft.com",
            "ubi.com",
            "ubisoftconnect.com",
            "uplay.com",
            "ubiservices.ubi.com",
            "ubisoft-orbit.s3.amazonaws.com",
            "ubisoft-orbit-legacy.s3.amazonaws.com",
            "ubistatic-a.akamaihd.net",
        ],
        ["DOMAIN-KEYWORD,ubisoft", "DOMAIN-KEYWORD,ubi.com"],
    ),
    (
        "gog", "GOG 加速", "💾", 80, "GOG Galaxy / 商店 / CDN",
        "https://www.gog.com/robots.txt",
        ["香港", "日本", "新加坡", "台湾", "欧洲"],
        [
            "gog.com",
            "gog-statics.com",
            "gogcdn.net",           # 游戏下载
            "gogalaxy.com",
            "gogapis.com",
            "gog.akamaized.net",
        ],
        ["DOMAIN-KEYWORD,gog-statics"],
    ),
    (
        "riot", "拳头加速", "🔫", 90, "LOL / 无畏契约 / Riot Client",
        "https://www.riotgames.com/robots.txt",
        ["香港", "台湾", "日本", "新加坡", "韩国"],
        [
            "riotgames.com",
            "riotcdn.net",
            "riotclient.com",
            "leagueoflegends.com",
            "leagueoflegendstw.com",
            "valorant.com",
            "playvalorant.com",
            "rgpub.io",
        ],
        ["DOMAIN-KEYWORD,riotgames", "DOMAIN-KEYWORD,riotcdn"],
    ),
    (
        "rockstar", "Rockstar 加速", "⭐", 100, "Rockstar Launcher / GTA / 大镖客",
        "https://www.rockstargames.com/robots.txt",
        ["香港", "日本", "新加坡", "美国", "欧洲"],
        [
            "rockstargames.com",
            "rockstarwarehouse.com",
            "rockstarsocialclub.com",
            "socialclub.rockstargames.com",
            "rsg.sc.llnwd.net",
            "prod.telemetry.ros.rockstargames.com",
        ],
        ["DOMAIN-KEYWORD,rockstargames"],
    ),
    (
        "playstation", "PlayStation 加速", "🎮", 110, "PS App / PSN 商店 / 串流",
        "https://www.playstation.com/robots.txt",
        ["香港", "日本", "台湾", "新加坡", "美国"],
        [
            "playstation.com",
            "playstation.net",
            "sonyentertainmentnetwork.com",
            "psnow.playstation.com",
        ],
        ["DOMAIN-KEYWORD,playstation"],
    ),
)


def _launcher_builtins() -> list[dict[str, Any]]:
    """把 _LAUNCHERS 展开成和内置场景同形状的模块（自动子组 + 偏好地区）。"""
    out: list[dict[str, Any]] = []
    for key, title, icon, order, desc, probe, regions, domains, rules in _LAUNCHERS:
        out.append(normalize({
            "key": key,
            "title": title,
            "desc": desc,
            "kind": "scene",
            "builtin": True,
            "icon": icon,
            "order": order,
            # 和 GitHub 一样带自动子组：节点池里总有死节点，钉死一个出口
            # 会让整个启动器打不开；自动子组按探测结果挑活的。
            "group": {
                "type": "select",
                "auto": True,
                "auto_title": title.replace("加速", "自动"),
                "probe": probe,
                "interval": 180,
                "timeout": 3000,
            },
            "prefer_regions": list(regions),
            "match": {"domains": list(domains), "rules": list(rules)},
        }))
    return out


def _read_mark(path: Path) -> set[str]:
    """读「已提案过的内置模块」清单。文件不存在/坏了都返回空集合。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return {ln.strip() for ln in text.splitlines() if ln.strip()}


def _write_mark(path: Path, keys: set[str]) -> None:
    try:
        path.write_text("\n".join(sorted(keys)) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("写内置模块清单失败 %s: %s", path, e)


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
    mark = d / SEEDED_KEYS
    # 1.0.0 之前的机器只有 .seeded，没有 key 清单：那时出厂的就 steam/github，
    # 直接按当时的事实补一份清单，别把它们当成「新模块」再写一遍。
    done = _read_mark(mark)
    if seeded.exists() and not done:
        done = {"steam", "github"}
        _write_mark(mark, done)
    made = 0
    legacy = {
        "steam": ("steam_enabled", "steam_node", "steam_prefer_regions"),
        "github": ("github_enabled", "github_node", "github_prefer_regions"),
    }
    for mod in builtins():
        k = mod["key"]
        # 已经播种过、且这个 key 当初就有的：说明用户见过它（可能主动删了），
        # 不再补。只有「升级后才新增的出厂模块」才走补种。
        if seeded.exists() and k in done:
            continue
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
        # 写成功与否都记进清单：用户自己删的不要下次又冒出来。
        done.add(k)
    _write_mark(mark, done)
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
