"""验证：切换节点后自动补出口 IP / 城市。用法: python tools/probe_test.py"""
import json
import time
import urllib.request

B = "http://127.0.0.1:7892"


def get(p):
    return json.loads(urllib.request.urlopen(B + p, timeout=25).read())


def post(p, d):
    r = urllib.request.Request(
        B + p, data=json.dumps(d).encode(), headers={"Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(r, timeout=60).read())


def main() -> int:
    st = get("/api/state")["state"]
    cands = [n["name"] for n in st["nodes"] if "香港-中转 01" in n["name"]]
    if not cands:
        print("找不到目标节点")
        return 1
    target = cands[0]
    before = [n for n in st["nodes"] if n["name"] == target][0]
    print(f"目标: {target}")
    print(f"切换前: city={before.get('city')!r} ip={before.get('purity_ip')!r}")

    print("select:", post("/api/select", {"name": target}).get("ok"))
    got = None
    for i in range(16):
        time.sleep(1)
        s = get("/api/state")["state"]
        nd = [n for n in s["nodes"] if n["name"] == target][0]
        busy = s.get("busy") or ""
        pg = s.get("progress") or {}
        print(f"  {i + 1:2d}s busy={busy!r} progress={pg.get('kind')} {pg.get('done')}/{pg.get('total')}")
        if nd.get("city") or nd.get("purity_ip"):
            got = nd
            break

    if got:
        print(f"\n✓ 自动补全: {got['city']} · {got['purity_ip']} · 纯净度 {got['purity']}")
        s = get("/api/state")["state"]
        n_city = sum(1 for n in s["nodes"] if n.get("city"))
        print(f"当前有城市的节点: {n_city}/{len(s['nodes'])}")
        return 0
    print("\n✗ 16 秒内没拿到 city/ip")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
