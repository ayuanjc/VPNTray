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

from .paths import CONFIG, DATA, UI_DIR, ensure_dirs

log = logging.getLogger("config")

# 最前面的内网 / 本地直连，避免访问 NAS、打印机、本机服务被代理
LAN_RULES = [
    "DOMAIN-SUFFIX,local,DIRECT",
    "DOMAIN-SUFFIX,lan,DIRECT",
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
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


def build(sub: dict[str, Any], settings) -> dict[str, Any]:
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
        ai_node = str(settings.get("ai_node") or "")
        members = ([ai_node] if ai_node in node_names else []) + \
                  [n for n in node_names if n != ai_node]
        groups.insert(0, {
            "name": AI_GROUP,
            "type": "select",
            "proxies": members,
        })
    sub = {**sub, "proxy-groups": groups}

    # —— 规则拼装 ——
    sub_rules = [r for r in sub["rules"] if not r.upper().startswith("MATCH")]
    out_rules: list[str] = []
    out_rules += LAN_RULES
    # AI 域名优先级高于订阅规则，避免被分到「国际媒体」之类的乱切组
    if ai_on:
        out_rules += [f"DOMAIN-SUFFIX,{d},{AI_GROUP}" for d in AI_DOMAINS]
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
        "tcp-concurrent": True,
        "find-process-mode": "strict",
        "external-controller": f"127.0.0.1:{_port(settings, 'controller_port', 9091)}",
        "secret": settings.get("secret") or "",
        "profile": {
            "store-selected": True,
            "store-fake-ip": True,
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

    cfg["geodata-mode"] = False
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
