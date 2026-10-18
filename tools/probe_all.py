"""把全部节点的出口 IP / 城市 / 纯净度跑一遍，跑完输出排行。

用法：python tools/probe_all.py [--limit N]
只打本地控制台的 API，跑的过程中程序会不停切节点，别在这期间干别的事。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:7892"


def api(path: str, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json", "Host": "127.0.0.1:7892"},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def main() -> int:
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    st = api("/api/state")["state"]
    nodes = st.get("nodes") or []
    targets = [n["name"] for n in nodes if n.get("type") not in ("Selector", "Direct", "Reject")]
    scope = "all"
    if "--retry" in sys.argv:
        # 只补测：有延迟但还没拿到纯净度的（通得了但上次查询失败）
        targets = [n["name"] for n in nodes
                   if n.get("purity") is None and n.get("delay")]
        scope = "names"
        if not targets:
            print("没有需要补测的节点")
            return 0
    if limit:
        targets = targets[:limit]
    print(f"目标节点 {len(targets)} 个，开始检测…", flush=True)

    t0 = time.time()
    payload = {"scope": scope}
    if scope == "names":
        payload["names"] = targets
    elif limit:
        payload["limit"] = limit
    r = api("/api/purity", payload)
    if not r.get("ok"):
        print("启动失败:", r)
        return 1

    last = ""
    while True:
        time.sleep(3)
        st = api("/api/state")["state"]
        pg = st.get("progress") or {}
        busy = st.get("busy") or ""
        cur = f"{pg.get('kind','')} {pg.get('done',0)}/{pg.get('total',0)} {pg.get('text','')} | {busy}"
        if cur != last:
            last = cur
            done, total = pg.get("done", 0), pg.get("total", 0)
            pct = f" {done*100//total}%" if total else ""
            print(f"  [{time.time()-t0:5.0f}s] {pg.get('text') or busy}{pct}", flush=True)
        if not pg.get("kind") and not busy:
            break
        if time.time() - t0 > 60 * 25:
            print("超时退出轮询")
            break

    st = api("/api/state")["state"]
    rows = []
    for n in st.get("nodes") or []:
        rows.append({
            "name": n.get("name"),
            "region": n.get("region"),
            "delay": n.get("delay"),
            "ip": n.get("purity_ip"),
            "city": n.get("city"),
            "cc": n.get("cc"),
            "purity": n.get("purity"),
            "grade": n.get("purity_grade"),
            "mbps": n.get("mbps"),
        })

    out = ROOT / "data" / "probe_all.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    have_ip = [x for x in rows if x["ip"]]
    have_p = [x for x in rows if x["purity"] is not None]
    print(f"\n用时 {(time.time()-t0)/60:.1f} 分钟 · 有 IP {len(have_ip)}/{len(rows)} · "
          f"有纯净度 {len(have_p)}/{len(rows)}")
    print(f"明细已存 {out}")

    # 纯净度排行（同分按延迟）
    have_p.sort(key=lambda x: (-(x["purity"] or 0), x["delay"] or 9999))
    print("\n=== 纯净度排行 ===")
    for x in have_p:
        print(f"  {x['purity']:3d} {x['grade'] or '':4s} {x['delay'] if x['delay'] else '--':>5}ms  "
              f"{x['name']}  ·  {x['city'] or ''} {x['ip'] or ''}")

    missing = [x for x in rows if not x["ip"]]
    if missing:
        print(f"\n=== 没拿到 IP 的 {len(missing)} 个 ===")
        for x in missing:
            print(f"  {x['name']}  delay={x['delay']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
