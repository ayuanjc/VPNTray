# -*- coding: utf-8 -*-
"""生成发行版预置数据：settings.json + sub_cache.yaml"""
import io, sys, os, json, time, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import requests
from vpntray import subscription, settings as S

URL = "https://cat.cn-ping.com/sabusuku?token=295d2311ca04445d5ee08db01e4c65bb"
STAGE = r"C:\Users\ayuan\Desktop\AI\intert\dist\VPNTray-热点特化版"
data = os.path.join(STAGE, "data")
os.makedirs(data, exist_ok=True)

ss = requests.Session(); ss.trust_env = False
r = ss.get(URL, timeout=25, headers={"User-Agent": "ClashMetaForAndroid/2.11.6"})
r.raise_for_status()
raw = r.content
sub = subscription.normalize(subscription.parse(raw))
info = sub.get("info") or []

d = dict(S.DEFAULTS)
d.update({
    "sub_url": URL,
    "subs": [{
        "name": "手机热点专属",
        "url": URL,
        "info": info,
        "nodes": len(sub["proxies"]),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }],
    "sub_active": 0,
    "sub_hash": hashlib.sha256(raw).hexdigest(),
    "mode": "rule",
    "main_group": "节点选择",
    "system_proxy": True,
    "start_minimized": True,
    "autostart": False,
    "ai_enabled": True,
    "ai_node": "",
    "heal": True,
    "notify": True,
    "log_level": "warning",
    # 关掉自动更新：上游 repo 发布的是标准版，覆盖之后热点特化的改动就没了
    "auto_update": False,
    "update_repo": "",
    "update_interval_hours": 0,
    "agreed_version": "2.2.0",
    "edition": "mobile",
})
open(os.path.join(data, "sub_cache.yaml"), "wb").write(raw)
open(os.path.join(data, "settings.json"), "w", encoding="utf-8", newline="\n").write(
    json.dumps(d, ensure_ascii=False, indent=2))
print("nodes", len(sub["proxies"]), "bytes", len(raw))
print("info", info)
print("wrote", data)
