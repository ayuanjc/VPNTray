"""开发/自检用：打印生成后的配置摘要与实际运行状态。

用法:
    python tools/inspect.py            # 看配置文件
    python tools/inspect.py live       # 看内核实时状态
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from vpntray.api import MihomoAPI  # noqa: E402
from vpntray.settings import Settings  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def show_config() -> None:
    c = yaml.safe_load((ROOT / "data" / "config.yaml").read_text(encoding="utf-8"))
    keys = ("mixed-port", "allow-lan", "bind-address", "mode", "external-controller",
            "find-process-mode", "geodata-mode", "unified-delay", "tcp-concurrent")
    print("── 基础 ──")
    for k in keys:
        print(f"  {k}: {c.get(k)}")
    print(f"  proxies: {len(c['proxies'])}   rules: {len(c['rules'])}")
    print("── 策略组 ──")
    for g in c["proxy-groups"]:
        first = g["proxies"][0] if g["proxies"] else None
        print(f"  {g['name']:<10} {g['type']:<10} n={len(g['proxies']):<4} first={first}")
    print("── 规则头 ──")
    for r in c["rules"][:5]:
        print("  " + r)
    print("── 规则尾 ──")
    for r in c["rules"][-5:]:
        print("  " + r)
    print("── 节点样例 ──")
    for p in c["proxies"][:5]:
        print(f"  {p['name']}  ({p['type']} {p.get('server')}:{p.get('port')})")


def show_live() -> None:
    s = Settings()
    api = MihomoAPI(port=int(s["controller_port"]), secret=s["secret"])
    if not api.alive():
        print("内核未运行")
        return
    print("version:", api.version())
    print("configs.mode:", api.configs().get("mode"))
    proxies = api.proxies()
    group = s.get("main_group", "节点选择")
    g = proxies.get(group, {})
    print(f"主组 {group}: now={g.get('now')} n={len(g.get('all') or [])}")
    c = api.connections()
    print("traffic:", json.dumps(
        {"up": c.get("uploadTotal"), "down": c.get("downloadTotal"),
         "conns": len(c.get("connections") or [])}, ensure_ascii=False))
    delays = api.group_delay(group, s.get("delay_url"), int(s.get("delay_timeout_ms", 5000)))
    ok = dict(sorted(delays.items(), key=lambda kv: kv[1]))
    print(f"测速: {len(ok)} 个可用")
    for k, v in list(ok.items())[:8]:
        print(f"  {v:>5}ms {k}")


if __name__ == "__main__":
    show_live() if len(sys.argv) > 1 and sys.argv[1] == "live" else show_config()
