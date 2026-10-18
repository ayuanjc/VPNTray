"""等纯净度检测跑完，然后打印某国节点的 IP 定位结果。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7892"
CC = sys.argv[1] if len(sys.argv) > 1 else "JP"


def state() -> dict:
    req = urllib.request.Request(BASE + "/api/state", headers={"Host": "127.0.0.1:7892"})
    d = json.loads(urllib.request.urlopen(req, timeout=8).read())
    return d.get("state") or d


def main() -> int:
    t0 = time.time()
    last = ""
    while time.time() - t0 < 300:
        p = (state().get("progress") or {})
        if p.get("kind"):
            line = f"{p['kind']} {p.get('done')}/{p.get('total')} {p.get('text', '')}"
            if line != last:
                print(line)
                last = line
        elif time.time() - t0 > 6:
            print("检测结束")
            break
        time.sleep(4)

    nodes = [n for n in state()["nodes"] if n.get("cc") == CC]
    print(f"\n{CC} 节点定位情况（{len(nodes)} 个）:")
    located = 0
    for n in nodes:
        if n.get("lat") is not None:
            located += 1
        print(f"  {n['name'][:26]:28} lat={n.get('lat')} lon={n.get('lon')} "
              f"city={n.get('city') or '-':16} purity={n.get('purity')}")
    print(f"\n有坐标: {located}/{len(nodes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
