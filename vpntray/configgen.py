"""由订阅生成 mihomo 运行配置。

设计原则：
- 端口 / 控制器 / secret 由本程序接管，避免与机器上其它客户端冲突；
- 只监听 127.0.0.1，不对外暴露；
- 分流：内网直连 → 订阅规则 → GEOSITE,cn / GEOIP,CN 直连 → 其余走主策略组；
- fake-ip + sniffer 组合，保证国内站点判定准确、国外站点 TLS SNI 正确。
"""
from __future__ import annotations

import logging
import os
import shutil
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
SCENES: list[dict[str, Any]] = [
    {
        "key": "steam",
        "title": "Steam 加速",
        "desc": "商店 / 社区 / 下载 CDN",
        "settings_key": "steam_enabled",
        "node_key": "steam_node",
        "prefer_key": "steam_prefer_regions",
        "prefer": ["香港", "台湾", "日本", "韩国", "新加坡"],
        "domains": [
            "steampowered.com",       # 商店 / 登录 / 支付
            "steamcommunity.com",     # 社区、创意工坊、市场
            "steamstatic.com",        # 图片与静态资源
            "steamcontent.com",       # 下载 CDN（游戏本体）
            "steamusercontent.com",   # 工坊内容
            "steamgames.com",
            "steamserver.net",
            "steam-chat.com",
            "steamconnecttest.com",
            "steamdeck.com",
            "s.team",                 # 短链与登录跳转
        ],
    },
    {
        "key": "github",
        "title": "GitHub 加速",
        "desc": "git / Release / Pages / 容器镜像",
        "settings_key": "github_enabled",
        "node_key": "github_node",
        "prefer_key": "github_prefer_regions",
        "prefer": ["香港", "日本", "新加坡", "台湾", "美国"],
        # 自动子组：一个 select 组只能钉死一个出口，钉到的节点一挂，整个
        # GitHub 就打不开（这台机器 100 个节点里 88 个是死的，机场中转段
        # 大面积失效）。所以组里第一位放一个 fallback 子组，它拿 GitHub
        # 自己的地址每 180s 探一次，探测超时 3s，第一个通的就是出口。
        "auto": True,
        "auto_title": "GitHub 自动",
        "probe": "https://github.com/robots.txt",
        "domains": [
            "github.com",             # 含 api / gist / codeload / uploads / npm.pkg
            "githubusercontent.com",  # raw / objects / media(LFS) / release-assets
            "githubassets.com",       # 页面静态资源
            "github.io",              # Pages
            "ghcr.io",                # 容器镜像
            "github.dev",             # 网页编辑器
            "githubcopilot.com",
            "github.blog",
            "githubstatus.com",       # 状态页
        ],
        # Release 老式直链会 302 到 S3，那些域名不属于上面任何一个后缀，
        # 掉到 MATCH 就按 Netlfix 的落地点绕地球一圈，下载速度掉到几十 KB/s。
        "extra_rules": [
            "DOMAIN-KEYWORD,github-production-release-asset",
            "DOMAIN,github-cloud.s3.amazonaws.com",
        ],
    },
]


AUTO_SUFFIX = " 自动"


def scene_by_key(key: str) -> dict[str, Any] | None:
    for sc in SCENES:
        if sc["key"] == key:
            return sc
    return None


def auto_name(sc: dict[str, Any]) -> str:
    """场景的自动子组名（"GitHub 自动"）。关掉自动的组没有这个名字。"""
    return str(sc.get("auto_title") or f"{sc['title']}{AUTO_SUFFIX}")

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
    # 注意：fake-ip 模式下不要用境外 DoH 做 nameserver/policy，
    # 那些 DoH 查询本身要经过代理，会在国内网络里卡 5 秒超时。
    # 一律用国内 DNS，真实解析交给代理节点去做。
}

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

    pref: list[str] = []
    try:
        pref = list(settings.get(sc["prefer_key"]) or [])
    except Exception:  # noqa: BLE001
        pref = []
    pref = pref or list(sc["prefer"])

    def rank(n: str) -> int:
        reg = _hs.region_of(n)
        for i, w in enumerate(pref):
            if w and (w == reg or w in n):
                return i
        # 没命中偏好地区的不剔除（订阅节点命名千奇百怪），排后面就行
        return len(pref) + 5

    pinned = str(settings.get(sc["node_key"]) or "")
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
    if not members or not sc.get("auto") or not bool(settings.get("scene_auto", True)):
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


def build(sub: dict[str, Any], settings,
          delays: dict[str, Any] | None = None) -> dict[str, Any]:
    """sub: subscription.normalize() 的结果。"""
    if not sub.get("proxy-groups"):
        raise ValueError("订阅里没有可用的策略组")
    main_group = settings.get("main_group") or "节点选择"
    group_names = {g["name"] for g in sub["proxy-groups"]}
    if main_group not in group_names:
        selects = [g["name"] for g in sub["proxy-groups"] if g.get("type") == "select"]
        main_group = selects[0] if selects else sub["proxy-groups"][0]["name"]
        settings["main_group"] = main_group
        log.warning("主策略组回退为 %s", main_group)

    # —— 自动测速组的健康检查改成 HTTPS ——
    # 机场默认常用 http://cp.cloudflare.com，很多节点会偶发 HEAD 失败被误判，
    # 表现为「自动选择」一会儿通一会儿不通。
    probe_url = settings.get("delay_url") or "https://www.gstatic.com/generate_204"
    probe_url = probe_url.replace("http://www.gstatic.com", "https://www.gstatic.com")
    groups: list[dict[str, Any]] = []
    for g in sub["proxy-groups"]:
        g = dict(g)
        if g.get("type") in {"url-test", "fallback", "load-balance"}:
            g["url"] = probe_url
            g["interval"] = 300
            g["tolerance"] = 50
            g["lazy"] = False
        groups.append(g)

    # —— AI 专用组（放在最前面，方便 UI 里找到）——
    # 注意：建组与插规则必须用同一个条件。曾经节点全被剔成空列表时组没建、
    # 规则却还在引用它，mihomo 直接报 group not found，内核起不来。
    node_names = [p["name"] for p in sub["proxies"]]
    ai_on = bool(settings.get("ai_enabled", True)) and bool(node_names)
    if ai_on:
        from . import hotspot as _hots
        ai_node = str(settings.get("ai_node") or "")
        # 只把「不会被 OpenAI 拒绝」的地区放进 AI 组。
        # 香港节点延迟再低也过不去：api.openai.com 在握手阶段就被 RST，
        # 表现出来是 SSL EOF，而 google / github 全通 —— 所以必须按地区剔。
        pool = [n for n in node_names if not _hots.is_blocked_region(n, settings)]
        if len(pool) < 2:            # 剔空了就别剔，否则组是空的、规则还引用它
            pool = list(node_names)
        # 组内顺序 = 界面里从上到下的顺序。按偏好地区排，让「能用的」
        # 全堆在最上面，手动切的时候不用在 100 个节点里翻。
        pool.sort(key=lambda x: _hots.region_rank(x, settings))
        members = ([ai_node] if ai_node in pool else []) + \
                  [x for x in pool if x != ai_node]
        groups.insert(0, {
            "name": AI_GROUP,
            "type": "select",
            "proxies": members,
        })

    # —— 场景加速组（Steam / GitHub）——
    # 和 AI 组一样有「建了组却没插规则 / 插了规则却没建组」的坑：
    # mihomo 遇到引用不存在的组会直接起不来，所以两边用同一个 scene_on 判断。
    scene_on: set[str] = set()
    scene_groups: list[dict[str, Any]] = []
    for sc in SCENES:
        if not bool(settings.get(sc["settings_key"], True)) or not node_names:
            continue
        members, auto = _scene_layout(sc, node_names, settings, delays)
        if not members:
            continue
        if auto and len(auto) >= 2:
            aname = auto_name(sc)
            scene_groups.append({
                "name": aname,
                "type": "fallback",
                # 探测地址用场景自己的域名：拿 gstatic 探出来的「快」，在
                # GitHub 上完全可能 502 / TLS 被掐，测了等于没测。
                "url": sc.get("probe") or "https://www.gstatic.com/generate_204",
                "interval": 180,
                "timeout": 3000,     # 3s 还没握手完的节点在 GitHub 上就是不可用
                "lazy": False,
                "proxies": auto,
            })
            pinned = str(settings.get(sc["node_key"]) or "")
            if pinned and pinned in members:
                # 用户明确钉了节点：钉的优先，自动组退到第二位（还能手动选回来）
                head = [pinned, aname] + [m for m in members if m != pinned]
            else:
                # 没钉（或钉的是自动组本身）：默认走自动组，组内自己挑活的
                head = [aname] + [m for m in members if m != aname]
            scene_groups.append({"name": sc["title"], "type": "select", "proxies": head})
        else:
            scene_groups.append({"name": sc["title"], "type": "select", "proxies": members})
        scene_on.add(sc["title"])
    for i, g in enumerate(scene_groups):
        groups.insert((1 if ai_on else 0) + i, g)
    if scene_on:
        log.info("场景加速：%s", "、".join(sorted(scene_on)))

    sub = {**sub, "proxy-groups": groups}

    # —— 规则拼装 ——
    sub_rules = [r for r in sub["rules"] if not r.upper().startswith("MATCH")]
    out_rules: list[str] = []
    out_rules += LAN_RULES
    # AI 域名优先级高于订阅规则，避免被分到「国际媒体」之类的乱切组
    if ai_on:
        out_rules += [f"DOMAIN-SUFFIX,{d},{AI_GROUP}" for d in AI_DOMAINS]
    # 场景加速同样排在订阅规则前面：订阅把这些域名丢进「国际媒体」时，
    # 那条线按 Netflix 的落地点走，Steam 下载和 git clone 会慢到没法用。
    for sc in SCENES:
        if sc["title"] in scene_on:
            out_rules += [f"DOMAIN-SUFFIX,{d},{sc['title']}" for d in sc["domains"]]
            # 场景自带的非后缀规则（Release 的 S3 直链用 KEYWORD 才盖得住）
            out_rules += [f"{pat},{sc['title']}" for pat in (sc.get("extra_rules") or [])]
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

    # —— DNS ——
    dns = dict(DEFAULT_DNS)
    sub_dns = sub.get("dns") or {}
    for k in ("default-nameserver", "nameserver", "proxy-server-nameserver", "enhanced-mode",
              "fake-ip-range", "fake-ip-filter", "ipv6", "use-hosts"):
        if sub_dns.get(k):
            dns[k] = sub_dns[k]
    if not dns.get("fake-ip-filter"):
        dns["fake-ip-filter"] = FAKE_IP_FILTER
    # 境外 DoH 在国内网络上会先卡超时，统一交给国内 DNS + fake-ip
    if dns.pop("nameserver-policy", None):
        log.info("已移除订阅中的 nameserver-policy（避免境外 DoH 卡顿）")

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
        "proxy-groups": sub["proxy-groups"],
        "rules": out_rules,
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


def _port(settings, key: str, default: int) -> int:
    """端口必须是 1024..65535 的整数，脏值回落默认值。"""
    try:
        v = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return v if 1024 <= v <= 65535 else default


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
