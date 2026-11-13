"""由订阅生成 mihomo 运行配置。

设计原则：
- 端口 / 控制器 / secret 由本程序接管，避免与机器上其它客户端冲突；
- 只监听 127.0.0.1，不对外暴露；
- 分流：内网直连 → 订阅规则 → GEOSITE,cn / GEOIP,CN 直连 → 其余走主策略组；
- fake-ip + sniffer 组合，保证国内站点判定准确、国外站点 TLS SNI 正确。
"""
from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
from typing import Any

import yaml

from .paths import CONFIG, DATA, MMDB, UI_DIR, ensure_dirs

log = logging.getLogger("config")

# 最前面的内网 / 本地直连，避免访问 NAS、打印机、本机服务被代理
LAN_RULES = [
    "DOMAIN-SUFFIX,local,DIRECT",
    "DOMAIN-SUFFIX,lan,DIRECT",
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    # 校园网用的是 172.32/16（Dr.COM 认证门户 172.32.253.17）和 172.50/16（客户端网段），
    # 都不在 RFC1918 内；不显式直连的话，靠 GEOIP,CN 兜底，随时可能被发到境外节点。
    "IP-CIDR,172.32.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,172.50.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,169.254.0.0/16,DIRECT,no-resolve",
    "IP-CIDR,100.64.0.0/10,DIRECT,no-resolve",
    "IP-CIDR6,fe80::/10,DIRECT,no-resolve",
    "IP-CIDR6,::1/128,DIRECT,no-resolve",
]

# 国内基础服务补充（订阅规则之外的安全网，放订阅规则之前）
CN_EXTRA_RULES = [
    "DOMAIN-SUFFIX,cn,DIRECT",
    "DOMAIN-SUFFIX,wechat.com,DIRECT",
    "DOMAIN-SUFFIX,qq.com,DIRECT",
    "DOMAIN-SUFFIX,taobao.com,DIRECT",
    "DOMAIN-SUFFIX,tmall.com,DIRECT",
    "DOMAIN-SUFFIX,jd.com,DIRECT",
    "DOMAIN-SUFFIX,alipay.com,DIRECT",
    "DOMAIN-SUFFIX,alicdn.com,DIRECT",
    "DOMAIN-SUFFIX,bilibili.com,DIRECT",
    "DOMAIN-SUFFIX,baidu.com,DIRECT",
    "DOMAIN-SUFFIX,aliyun.com,DIRECT",
    "DOMAIN-SUFFIX,aliyuncs.com,DIRECT",
    "DOMAIN-SUFFIX,163.com,DIRECT",
    "DOMAIN-SUFFIX,126.net,DIRECT",
    "DOMAIN-SUFFIX,mi.com,DIRECT",
    "DOMAIN-SUFFIX,xiaomi.com,DIRECT",
    "DOMAIN-SUFFIX,huawei.com,DIRECT",
    "DOMAIN-SUFFIX,douyin.com,DIRECT",
    "DOMAIN-SUFFIX,bytedance.com,DIRECT",
    "DOMAIN-SUFFIX,zhihu.com,DIRECT",
    "DOMAIN-SUFFIX,csdn.net,DIRECT",
    "DOMAIN-SUFFIX,gitee.com,DIRECT",
    "DOMAIN-SUFFIX,pinduoduo.com,DIRECT",
    "DOMAIN-SUFFIX,meituan.com,DIRECT",
    "DOMAIN-SUFFIX,dianping.com,DIRECT",
    "DOMAIN-SUFFIX,ctrip.com,DIRECT",
    "DOMAIN-SUFFIX,12306.cn,DIRECT",
    "DOMAIN-SUFFIX,amap.com,DIRECT",
]

# ── AI 客户端（Claude Code / Codex / ChatGPT）专用 ──
# 这些域名单独走一个「AI 稳定」组，节点不会被自动优选/健康看护切来切去，
# 避免会话中途换 IP 触发风控。
AI_GROUP = "AI 稳定"
AI_DOMAINS = [
    "anthropic.com",          # api.anthropic.com / statsig.anthropic.com
    "claude.ai",
    "claudeusercontent.com",
    "claude.com",
    "openai.com",             # api.openai.com / auth.openai.com
    "chatgpt.com",
    "oaistatic.com",
    "oaiusercontent.com",
    "openrouter.ai",
    "cursor.sh",
    "cursor.com",
    "statsig.com",
    "featuregates.org",
    "sentry.io",
    "intercom.io",
    # 常见的中转/网关
    "packycode.com",
    "aihubmix.com",
    "closeai-asia.com",
]

# ── 场景加速：Steam / GitHub ──
# 和「AI 稳定」正好相反：这两个要的是**延迟、带宽、下载 CDN 命中**，IP 会变
# 也无所谓。所以各自一个 select 组，组内按偏好地区排序（港台日最省事），
# 用户钉住哪个就哪个；不钉就用组里第一个。
# 分流优先级：比订阅规则高（订阅常把它们丢进「国际媒体」，那条线往往绕地球
# 一圈），比 GEOSITE,cn 高（GitHub 在国内有 CDN，但 Pages / Release 资产
# 一律走境外，geo 分类不可靠）。
# 2.5.9 起场景不是代码里的常量，而是 data/modules/*.json 里的数据（见
# modules.py）。下面这些函数只负责「把模块数据翻译成 mihomo 的策略组和规则」，
# 所以用户自己加一个「Epic 加速」不需要改这里的任何一行，也不用重新出包。
#
# 场景要的是延迟、带宽、下载 CDN 命中，IP 会变也无所谓：各自一个独立 select
# 组，组内按偏好地区排序（港台日最省事），用户钉住哪个就哪个；不钉就用组里
# 第一个。分流优先级比订阅规则高（订阅常把它们丢进「国际媒体」，那条线往往
# 绕地球一圈），也比 GEOSITE,cn 高（GitHub 在国内有 CDN，但 Pages / Release
# 资产一律走境外，geo 分类不可靠）。
from . import modules as _modules  # noqa: E402


def all_modules() -> list[dict[str, Any]]:
    """全部模块（含纯规则模块），已按 order / 组名排好序。"""
    return _modules.load_all()


def scenes() -> list[dict[str, Any]]:
    """只有 kind=scene 的模块，也就是会真的建策略组的那些。"""
    return [m for m in _modules.load_all() if m.get("kind") == "scene"]


AUTO_SUFFIX = " 自动"


def scene_by_key(key: str) -> dict[str, Any] | None:
    return _modules.load(key)


def auto_name(sc: dict[str, Any]) -> str:
    """场景的自动子组名（"GitHub 自动"）。关掉自动的模块没有这个名字。"""
    g = sc.get("group") or {}
    return str(g.get("auto_title") or f"{sc['title']}{AUTO_SUFFIX}")

FAKE_IP_FILTER = [    "*.lan",
    "*.local",
    "*.localdomain",
    "+.local",
    "localhost.ptlogin2.qq.com",
    "localhost.sec.qq.com",
    "+.srv.nintendo.net",
    "+.stun.playstation.net",
    "xbox.*.microsoft.com",
    "+.xboxlive.com",
    "*.msftconnecttest.com",
    "*.msftncsi.com",
    "time.*.com",
    "ntp.*.com",
    "+.market.xiaomi.com",
    "*.n.n.srv.nintendo.net",
    "stun.*.*",
    "stun.*.*.*",
    "proxy.golang.org",
    "*.push.apple.com",
]

DEFAULT_DNS: dict[str, Any] = {
    "enable": True,
    "ipv6": False,
    "enhanced-mode": "fake-ip",
    "fake-ip-range": "198.18.0.1/16",
    "fake-ip-filter": FAKE_IP_FILTER,
    "default-nameserver": ["223.5.5.5", "119.29.29.29"],
    "nameserver": [
        "223.5.5.5",
        "119.29.29.29",
        "https://dns.alidns.com/dns-query",
        "https://doh.pub/dns-query",
    ],
    "proxy-server-nameserver": ["223.5.5.5", "119.29.29.29"],
    # 注意：fake-ip 模式下不要用「不经代理」的境外 DoH 做 nameserver，
    # 那些查询在国内网络里会卡 5 秒超时。真实解析本来交给代理节点去做。
    # 但**交易域名是例外**，见下面 TRADE_DNS_DOMAINS：国内 DNS 会返回投毒结果，
    # 必须用「带 #策略组 的 DoH」，让查询本身走代理出去。
}

# ── 交易域名的 DNS 投毒护栏 ───────────────────────────────────────────
# 2026-11-13 的实况：国内 DNS 对 www.okx.com / app.okx.com 返回
#   awscn.okpool.top → 169.254.0.2（APIPA 链路本地地址，永远没有服务器）
# 而 sniffer.force-dns-mapping=true 要求 mihomo 先解析再连，于是客户端连到
# 黑洞地址、TLS 拿不到 ServerHello，五路全部报
#   Cannot connect to host www.okx.com:443 ssl:default [None]
# 同时节点延迟测试依然 120ms 全绿 —— 因为测速是节点侧解析，走的是另一条路径。
# 结论：交易域名不能相信任何国内 DNS（运营商 / 阿里 / 腾讯 / 114 一样会投毒）。
TRADE_DNS_DOMAINS: tuple[str, ...] = ("+.okx.com", "+.binance.com")
TRADE_DNS_SERVERS: tuple[str, ...] = ("https://1.1.1.1/dns-query",
                                      "https://8.8.8.8/dns-query")


def trade_dns_policy(main_group: str) -> dict[str, list[str]]:
    """交易域名 → 经代理的 DoH 列表。main_group 为空时返回 {}（不写策略）。

    "#节点选择" 是 mihomo 的「这条 DNS 查询走哪个出口」语法，加上它以后
    境外 DoH 不再从国内直连，实测 0.33~0.60s，不会卡 5 秒。反过来，少了
    这一层，「一律用国内 DNS」的护栏本身就会把交易流量送进投毒结果里。
    """
    grp = str(main_group or "").strip()
    if not grp:
        return {}
    return {dom: [f"{srv}#{grp}" for srv in TRADE_DNS_SERVERS]
            for dom in TRADE_DNS_DOMAINS}

SNIFFER: dict[str, Any] = {
    "enable": True,
    "force-dns-mapping": True,
    "parse-pure-ip": True,
    "override-destination": False,
    "sniff": {
        "HTTP": {"ports": [80, "8080-8880"], "override-destination": True},
        "TLS": {"ports": [443, 8443]},
        "QUIC": {"ports": [443, 8443]},
    },
    "skip-domain": ["Mijia Cloud", "+.push.apple.com", "+.apple.com"],
}


def _scene_members(sc: dict[str, Any], node_names: list[str], settings) -> list[str]:
    """场景组里的成员顺序：钉住的排第一，其后按偏好地区，其余按名字。

    select 组的第一个就是默认出口，所以「排序」= 「默认用什么节点」。
    Steam 下载靠港台日，GitHub git 传输靠日本/新加坡，各有各的偏好，
    所以偏好列表按场景分开配置（settings 里可覆盖）。
    """
    from . import hotspot as _hs

    # 偏好地区属于模块自己的数据（用户/AI 都能改），不再走 settings 的 *_prefer_regions
    pref: list[str] = [str(x) for x in (sc.get("prefer_regions") or []) if str(x).strip()]
    if not pref:
        from .modules import builtins as _builtin_mods
        base = next((m for m in _builtin_mods() if m["key"] == sc.get("key")), None)
        pref = list((base or {}).get("prefer_regions") or [])

    def rank(n: str) -> int:
        reg = _hs.region_of(n)
        for i, w in enumerate(pref):
            if w and (w == reg or w in n):
                return i
        # 没命中偏好地区的不剔除（订阅节点命名千奇百怪），排后面就行
        return len(pref) + 5

    pinned = str(sc.get("pinned") or "")
    rest = sorted((n for n in node_names if n != pinned), key=lambda n: (rank(n), n))
    return ([pinned] if pinned and pinned in node_names else []) + rest


def _scene_layout(sc: dict[str, Any], node_names: list[str], settings,
                  delays: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """返回 (select 组成员, 自动子组成员；不需要自动组时为空)。

    自动子组只装「上次测速真拿到响应」的节点，并按延迟从小到大排：
    fallback 组的语义就是「按顺序找第一个健康的」，所以顺序 = 优先级，
    排前面的先被用到。没有测速数据（新装、刚换订阅）时退回全部节点，
    由组自己的健康检查去筛 —— 这样不会因为「还没测过」就把组建成空的。
    """
    members = _scene_members(sc, node_names, settings)
    grp = sc.get("group") or {}
    if not members or not grp.get("auto") or not bool(settings.get("scene_auto", True)):
        return members, []
    d = delays or {}

    def ms(n: str) -> float:
        try:
            v = float(d.get(n) or 0)
        except (TypeError, ValueError):
            return 0.0
        return v if v > 0 else 0.0

    fresh = [(ms(n), n) for n in members if ms(n) > 0]
    if len(fresh) >= 2:
        return members, [n for _, n in sorted(fresh)]
    if len(fresh) == 1:
        # 只有一个测通过：让它排第一，但别把其他人剔掉 —— 那次测速本身可能
        # 刚好撞上抖动，剔掉就再也没有被选中的机会了。
        only = fresh[0][1]
        return members, [only] + [n for n in members if n != only]
    # 完全没测过（新装 / 刚换订阅）：全量交给组自己的健康检查去筛
    return members, list(members)


def _main_group(sub: dict[str, Any], settings) -> str:
    """主策略组。settings 里记的那个不在订阅里，就退回第一个 select 并写回 settings。"""
    main_group = settings.get("main_group") or "节点选择"
    if main_group in {g["name"] for g in sub["proxy-groups"]}:
        return main_group
    selects = [g["name"] for g in sub["proxy-groups"] if g.get("type") == "select"]
    main_group = selects[0] if selects else sub["proxy-groups"][0]["name"]
    settings["main_group"] = main_group
    try:                      # 回退结果要落盘，否则每次启动都会重新 warning 一次
        settings.save()
    except Exception:         # noqa: BLE001    测试桩/只读环境放过
        pass
    log.warning("主策略组回退为 %s", main_group)
    return main_group


def _probe_url(settings) -> str:
    """自动测速组的健康检查地址。

    机场默认常用 http://cp.cloudflare.com，很多节点会偶发 HEAD 失败被误判，
    表现为「自动选择」一会儿通一会儿不通。
    """
    url = settings.get("delay_url") or "https://www.gstatic.com/generate_204"
    return url.replace("http://www.gstatic.com", "https://www.gstatic.com")


def _subscription_groups(sub: dict[str, Any], probe_url: str) -> list[dict[str, Any]]:
    """订阅自带的组；自动测速类统一改成 HTTPS 探测。"""
    groups: list[dict[str, Any]] = []
    for g in sub["proxy-groups"]:
        g = dict(g)
        if g.get("type") in {"url-test", "fallback", "load-balance"}:
            # 探测地址仍然覆盖（机场默认的 cp.cloudflare.com 常被 HEAD 打回 403，
            # 会把好节点误判成死的），但 interval / tolerance / lazy 尊重订阅自己的
            # 配置：以前一律改成 300s / 50ms / lazy=false，等于让所有组常驻健康检查，
            # 移动网络下是持续流量和耗电。
            g["url"] = probe_url
            g.setdefault("interval", 300)
            if g["type"] == "url-test":
                g.setdefault("tolerance", 50)
            g.setdefault("lazy", False)
        groups.append(g)
    return groups


def _ai_group(node_names: list[str], settings) -> dict[str, Any] | None:
    """AI 专用组（插在组列表最前面，方便 UI 里找到）。没开 / 没节点就返回 None。

    只把「不会被 OpenAI 拒绝」的地区放进 AI 组。香港节点延迟再低也过不去：
    api.openai.com 在握手阶段就被 RST，表现出来是 SSL EOF，而 google / github
    全通 —— 所以必须按地区剔。
    """
    if not bool(settings.get("ai_enabled", True)) or not node_names:
        return None
    from . import hotspot as _hots

    ai_node = str(settings.get("ai_node") or "")
    pool = [n for n in node_names if not _hots.is_blocked_region(n, settings)]
    if len(pool) < 2:            # 剔空了就别剔，否则组是空的、规则还引用它
        pool = list(node_names)
    # 组内顺序 = 界面里从上到下的顺序。按偏好地区排，让「能用的」全堆在最上面，
    # 手动切的时候不用在 100 个节点里翻。
    pool.sort(key=lambda x: _hots.region_rank(x, settings))
    members = ([ai_node] if ai_node in pool else []) + [x for x in pool if x != ai_node]
    return {"name": AI_GROUP, "type": "select", "proxies": members}


def _scene_groups(node_names: list[str], settings, delays) -> tuple[list[dict[str, Any]], set[str]]:
    """Steam / GitHub 场景组，外加「哪几个场景真的建了组」。

    返回的 set 必须同时喂给规则拼装：mihomo 遇到引用不存在的组会直接起不来，
    所以「建组」和「插规则」只能用同一个判断。
    """
    scene_on: set[str] = set()
    scene_groups: list[dict[str, Any]] = []
    if not node_names:
        return scene_groups, scene_on
    for sc in scenes():
        if not bool(sc.get("enabled", True)):
            continue
        members, auto = _scene_layout(sc, node_names, settings, delays)
        if not members:
            log.info("模块 %s 没有可用成员，本轮不建组", sc.get("key"))
            continue
        grp = sc.get("group") or {}
        gtype = str(grp.get("type") or "select")
        # 探测地址优先用模块自己的域名：拿 gstatic 探出来的「快」，在 GitHub 上
        # 完全可能 502 / TLS 被掐，测了等于没测。
        probe = str(grp.get("probe") or "") or "https://www.gstatic.com/generate_204"
        interval = int(grp.get("interval") or 180)
        timeout = int(grp.get("timeout") or 3000)
        pinned = str(sc.get("pinned") or "")
        if grp.get("auto") and gtype == "select" and len(auto) >= 2:
            aname = auto_name(sc)
            scene_groups.append({
                "name": aname,
                "type": "fallback",
                "url": probe,
                "interval": interval,
                "timeout": timeout,   # 3s 还没握手完的节点在 GitHub 上就是不可用
                "lazy": False,
                "proxies": auto,
            })
            if pinned and pinned in members:
                # 用户明确钉了节点：钉的优先，自动组退到第二位（还能手动选回来）
                head = [pinned, aname] + [m for m in members if m != pinned]
            else:
                # 没钉（或钉的是自动组本身）：默认走自动组，组内自己挑活的
                head = [aname] + [m for m in members if m != aname]
            scene_groups.append({"name": sc["title"], "type": "select", "proxies": head})
        elif gtype == "select":
            scene_groups.append({"name": sc["title"], "type": "select", "proxies": members})
        else:
            # 用户把组类型改成 url-test / fallback / load-balance 时按类型建，
            # 这几个参数是 mihomo 必填项，不填会直接报配置错。
            g: dict[str, Any] = {
                "name": sc["title"], "type": gtype, "proxies": members,
                "url": probe, "interval": interval, "timeout": timeout, "lazy": False,
            }
            if gtype == "url-test":
                g["tolerance"] = 50
            scene_groups.append(g)
        scene_on.add(sc["title"])
    return scene_groups, scene_on


def _rules(sub: dict[str, Any], main_group: str, ai_on: bool,
           scene_on: set[str], groups: list[dict[str, Any]] | None = None) -> list[str]:
    """规则拼装：本地规则 → AI → 场景 → 国内补充 → 订阅规则 → GEOSITE/GEOIP → MATCH。"""
    sub_rules = [r for r in sub["rules"] if not r.upper().startswith("MATCH")]
    out_rules: list[str] = []
    out_rules += LAN_RULES
    # AI 域名优先级高于订阅规则，避免被分到「国际媒体」之类的乱切组
    if ai_on:
        out_rules += [f"DOMAIN-SUFFIX,{d},{AI_GROUP}" for d in AI_DOMAINS]
    # 场景加速同样排在订阅规则前面：订阅把这些域名丢进「国际媒体」时，
    # 那条线按 Netflix 的落地点走，Steam 下载和 git clone 会慢到没法用。
    # 模块（场景 + 纯规则模块）：场景模块的目标就是自己的组名，纯规则模块
    # 指向 group.target（留空/写 $main 就是主组，也可以 DIRECT / REJECT / 订阅组名）。
    valid = {g["name"] for g in (groups or [])} | {"DIRECT", "REJECT", "PASS"}

    def _target(raw: str) -> str:
        t = str(raw or "").strip()
        if not t or t in ("$main", "MAIN", "PROXY"):
            return main_group
        if t in valid:
            return t
        log.warning("模块规则目标 %s 不存在，已改走主组 %s", t, main_group)
        return main_group

    for sc in all_modules():
        if not bool(sc.get("enabled", True)):
            continue
        match = sc.get("match") or {}
        if sc.get("kind") == "scene":
            if sc["title"] not in scene_on:
                continue
            tgt = sc["title"]
        else:
            tgt = _target((sc.get("group") or {}).get("target") or "")
        out_rules += [f"DOMAIN-SUFFIX,{d},{tgt}" for d in (match.get("domains") or [])]
        # 模块自带的非后缀规则（Release 的 S3 直链用 KEYWORD 才盖得住）
        out_rules += [f"{pat},{tgt}" for pat in (match.get("rules") or [])]
        for cidr in match.get("cidrs") or []:
            kind = "IP-CIDR6" if ":" in str(cidr) else "IP-CIDR"
            # no-resolve：让内核按 IP 直接判定，不要为了匹配再去解析一次域名
            out_rules.append(f"{kind},{cidr},{tgt},no-resolve")
    out_rules += [r for r in CN_EXTRA_RULES if r not in sub_rules]
    out_rules += sub_rules
    # 国内兜底：geosite:cn 域名直连（fake-ip 下比 GEOIP 更可靠）
    if not any(r.upper().startswith("GEOSITE,CN") for r in out_rules):
        idx = next(
            (i for i, r in enumerate(out_rules) if r.upper().startswith("GEOIP,CN")),
            len(out_rules),
        )
        out_rules.insert(idx, "GEOSITE,cn,DIRECT")
    if not any(r.upper().startswith("GEOIP,CN") for r in out_rules):
        out_rules.append("GEOIP,CN,DIRECT")
    out_rules.append(f"MATCH,{main_group}")
    return out_rules


_DNS_KEYS = ("default-nameserver", "nameserver", "proxy-server-nameserver", "enhanced-mode",
             "fake-ip-range", "fake-ip-filter", "ipv6", "use-hosts")


_FAKE_IP_POOLS = (
    ipaddress.ip_network("198.18.0.0/15"),   # RFC 2544 基准测试段，社区约定给 fake-ip
    ipaddress.ip_network("100.64.0.0/10"),   # 运营商级 NAT 段，家用网段极少落在这里
)


def _valid_fake_ip_range(value: Any) -> str | None:
    """订阅下发的 fake-ip 池必须是保留段，且不能比 /16 还碎。

    见过真实案例：机场把 fake-ip-range 写成 28.0.0.1/8 —— 那是公网地址段。
    整段 28.0.0.0/8 被当成虚拟 IP 池后，凡是规则判为 DIRECT 的域名都会拿到一个
    28.x.x.x 的假地址，然后客户端真的去连 28.x.x.x，表现就是「个别网站莫名其妙
    打不开」，而换一台机器又正常。这种脏值必须在本地被纠正，不能信任订阅。
    """
    try:
        net = ipaddress.ip_network(str(value), strict=False)
    except Exception:
        return None
    if net.version != 4 or net.prefixlen > 16:
        return None
    if not any(net.subnet_of(pool) for pool in _FAKE_IP_POOLS):
        return None
    return str(value)


_ipv6_ok: bool | None = None


def _ipv6_available() -> bool:
    """本机到底有没有 IPv6 出口。

    dns.ipv6=true 会让 mihomo 返回 AAAA，客户端于是先试 IPv6。没有 IPv6 出口的
    机器（校园网、部分移动网络）每次连接都要先等一次超时，体感就是「域名解析没问题
    但打开网页就是慢半拍」。探一次记住结果，探不通就是没有。
    """
    global _ipv6_ok
    if _ipv6_ok is not None:
        return _ipv6_ok
    _ipv6_ok = False
    for host in ("2400:3200::1", "240e:4c:4008::1"):
        s = None
        try:
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.settimeout(0.35)
            s.connect((host, 53))
            _ipv6_ok = True
            break
        except Exception:
            continue
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
    return _ipv6_ok


def _dns(sub: dict[str, Any], main_group: str = "") -> dict[str, Any]:
    """本地 DNS 基线 + 订阅覆盖 + 交易域名的反投毒策略。"""
    dns = dict(DEFAULT_DNS)
    sub_dns = sub.get("dns") or {}
    for k in _DNS_KEYS:
        if sub_dns.get(k):
            dns[k] = sub_dns[k]
    # —— 订阅可以覆盖上游，但不能覆盖掉"会不会把机器弄坏"的边界 ——
    raw_range = dns.get("fake-ip-range")
    good = _valid_fake_ip_range(raw_range)
    if good is None:
        log.warning("订阅的 fake-ip-range=%s 不是保留地址段，改用 %s（否则整段公网会被当成虚拟 IP）",
                    raw_range, DEFAULT_DNS["fake-ip-range"])
        dns["fake-ip-range"] = DEFAULT_DNS["fake-ip-range"]
    else:
        dns["fake-ip-range"] = good
    if dns.get("ipv6") and not _ipv6_available():
        log.warning("订阅开启了 dns.ipv6，但本机没有 IPv6 出口，已关闭（否则每次连接先卡一次 AAAA 超时）")
        dns["ipv6"] = False
    if not dns.get("fake-ip-filter"):
        dns["fake-ip-filter"] = FAKE_IP_FILTER
    # 订阅下发的 policy 一律丢弃：里面常是「境外 DoH 直连」，在国内网络上
    # 先卡 5 秒超时，等于把整机 DNS 拖死。policy 只由本程序按白名单重建。
    if dns.pop("nameserver-policy", None):
        log.info("已移除订阅中的 nameserver-policy（避免境外 DoH 直连卡顿）")
    trade = trade_dns_policy(main_group)
    if trade:
        # 只加交易域名，其余域名继续走国内 DNS + fake-ip（快且不会卡）
        dns["nameserver-policy"] = trade
        log.info("交易域名走代理 DoH：%s", "、".join(trade))
    return dns


def _assemble(sub: dict[str, Any], settings, dns: dict[str, Any],
              groups: list[dict[str, Any]], rules: list[str]) -> dict[str, Any]:
    """把各部分拼成最终配置：端口 / 档位参数 / 面板 / geodata 兜底。"""
    # —— 链路档位（hotspot.py）：手机热点下换保守参数 ——
    from . import hotspot as _hs

    prof = _hs.profile(settings)
    if prof["tier"] == "hotspot":
        log.info("检测到手机热点，使用保守档：tcp-concurrent=%s find-process=%s",
                 prof["tcp-concurrent"], prof["find-process-mode"])

    cfg: dict[str, Any] = {
        "mixed-port": _port(settings, "mixed_port", 7891),
        "port": 0,
        "socks-port": 0,
        "redir-port": 0,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": settings.get("mode") or "rule",
        "log-level": settings.get("log_level") or "warning",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": bool(prof["tcp-concurrent"]),
        "find-process-mode": prof["find-process-mode"],
        "external-controller": f"127.0.0.1:{_port(settings, 'controller_port', 9091)}",
        "secret": settings.get("secret") or "",
        "profile": {
            "store-selected": True,
            "store-fake-ip": True,
            # 移动网络中途 NAT 超时频繁，保活间隔按档位拉长
            "keep-alive-interval": int(prof["keep-alive-interval"]),
        },
        "dns": dns,
        "sniffer": SNIFFER,
        "tun": {
            "enable": bool(settings.get("tun")),
            "stack": "mixed",
            "auto-route": False,
            "dns-hijack": ["any:53"],
        },
        "proxies": sub["proxies"],
        "proxy-groups": groups,
        "rules": rules,
    }

    # 本地 Web 控制面板（若已放置 metacubexd / yacd）
    if (UI_DIR / "index.html").exists():
        cfg["external-ui"] = str(UI_DIR).replace("\\", "/")
        cfg["external-ui-name"] = "ui"

    # geodata-mode=False 走 country.mmdb；缺了这个文件 mihomo 不会报错退出，
    # 而是「Can't find MMDB, start download」去联网拉一份 —— 在这种破网络下
    # 就是几十秒没 API、最后被上层判成「内核启动后 API 无响应」。
    # 所以缺 mmdb 时改用 GeoIP.dat/GeoSite.dat（随包发的），彻底不联网。
    cfg["geodata-mode"] = not MMDB.exists()
    cfg["geo-auto-update"] = False
    return cfg


def build(sub: dict[str, Any], settings,
          delays: dict[str, Any] | None = None) -> dict[str, Any]:
    """sub: subscription.normalize() 的结果。

    只负责串流程，具体职责在下面几个私有函数里：
    主组回退 / 探测地址 / 订阅组 / AI 组 / 场景组 / 规则 / DNS / 最终拼装。
    """
    if not sub.get("proxy-groups"):
        raise ValueError("订阅里没有可用的策略组")
    main_group = _main_group(sub, settings)
    node_names = [p["name"] for p in sub["proxies"]]

    groups = _subscription_groups(sub, _probe_url(settings))
    ai = _ai_group(node_names, settings)
    if ai:
        groups.insert(0, ai)
    # AI 组占了第 0 位，场景组从 1 开始插，顺序即面板顺序。
    # 模块的组名是用户自己起的，可能和订阅里的组重名 —— mihomo 遇到同名组
    # 直接拒绝启动，所以这里必须去重（重名的模块本轮跳过并记日志）。
    scene_groups, scene_on = _scene_groups(node_names, settings, delays)
    # scene_on 由 _scene_groups 按「真的为这个模块建了组」给出，包含所有组类型。
    # 以前这里按 type == "select" 重算一遍，等于把 url-test / fallback /
    # load-balance 类型的模块规则全丢了（组建了、规则不插，流量落 MATCH）。
    taken = {g["name"] for g in groups}
    kept: list[dict[str, Any]] = []
    for g in scene_groups:
        if g["name"] in taken:
            log.warning("模块组名 %r 与订阅里的策略组重名，本轮跳过（改个组名即可）", g["name"])
            continue
        kept.append(g)
        taken.add(g["name"])
    base = 1 if ai else 0
    for i, g in enumerate(kept):
        groups.insert(base + i, g)
    # 重名被跳过的组要从 scene_on 里剔除，否则规则会指向一个不存在的组
    scene_on = {n for n in scene_on if n in {g["name"] for g in kept}}
    if kept:
        log.info("模块组：%s", "、".join(g["name"] for g in kept))

    rules = _rules(sub, main_group, ai is not None, scene_on, groups)
    return _assemble({**sub, "proxy-groups": groups}, settings,
                     _dns(sub, main_group), groups, rules)



from .settings import port_setting as _port  # 端口收敛的唯一实现


def write(cfg: dict[str, Any]) -> str:
    """原子写 config.yaml：崩在中间也不会让内核读到半截配置。"""
    ensure_dirs()
    text = yaml.safe_dump(
        cfg,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100000,
    )
    tmp = CONFIG.with_name(CONFIG.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if CONFIG.exists():
            try:
                shutil.copy2(CONFIG, CONFIG.with_name(CONFIG.name + ".bak"))
            except OSError:
                pass
        os.replace(tmp, CONFIG)
    except OSError as e:
        log.error("写入配置失败: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    log.info("配置已写入 %s (%d proxies, %d rules)",
             CONFIG, len(cfg["proxies"]), len(cfg["rules"]))
    return str(CONFIG)


def rule_summary(cfg: dict[str, Any]) -> dict[str, int]:
    from collections import Counter

    return dict(Counter(r.split(",")[0] for r in cfg["rules"]))
