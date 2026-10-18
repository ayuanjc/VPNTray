"""诊断「有延迟但查不到出口 IP」的节点：切过去等不同时间，看多久能通。"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = json.loads((ROOT / "data" / "settings.json").read_text(encoding="utf-8"))["secret"]
CTRL = "http://127.0.0.1:9091"
PROXY = "http://127.0.0.1:7891"
GROUP = "节点选择"


def api(path, method="GET", data=None):
    req = urllib.request.Request(
        CTRL + path, method=method,
        headers={"Authorization": "Bearer " + SECRET, "Content-Type": "application/json"},
        data=json.dumps(data).encode() if data else None,
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read()
    return json.loads(body) if body else None


def probe(timeout=9.0):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    with op.open("https://api.ipapi.is/", timeout=timeout) as r:
        d = json.loads(r.read())
    return d.get("ip"), (d.get("location") or {}).get("city")


def main() -> int:
    nodes = [
        "🇬🇧|英国-IEPL 01", "🇦🇪|阿联酋-IEPL 01", "🇻🇳|越南-IEPL 01",
        "🇮🇳|印度-IEPL 01", "🇹🇭|泰国-IEPL 01", "🇵🇭|菲律宾-IEPL 01 5倍消耗",
    ]
    for node in nodes:
        api("/proxies/" + urllib.parse.quote(GROUP), "PUT", {"name": node})
        line = f"{node:32s}"
        for wait in (0.5, 2.0, 4.0):
            time.sleep(wait)
            try:
                ip, city = probe()
                line += f" | 等{wait}s 通 {ip} {city}"
                break
            except Exception as e:  # noqa: BLE001
                line += f" | 等{wait}s {type(e).__name__}"
        print(line, flush=True)
    # 切回
    api("/proxies/" + urllib.parse.quote(GROUP), "PUT", {"name": "🇲🇾|马来西亚-IEPL 02"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
