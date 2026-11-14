#!/usr/bin/env python3
"""量 /api/state 的真实成本（性能预算门禁）。

用法：
    python -X utf8 tools/bench_state.py
    python -X utf8 tools/bench_state.py --port 7893 --n 50 --max-ms 12

退出码：0 = 达预算；1 = 超预算（p95 > --max-ms 或响应体 > --max-kb）；2 = 连不上。
只做 GET，不改状态，可以随便在部署脚本里跑。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request


def _get(url, timeout=5.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
            return r.status, (time.perf_counter() - t0) * 1000.0, body
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000.0, b""
    except OSError as e:
        return 0, (time.perf_counter() - t0) * 1000.0, str(e).encode()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7892)
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--max-ms", type=float, default=15.0)
    ap.add_argument("--max-kb", type=float, default=10.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    url = "http://%s:%d/api/state" % (a.host, a.port)
    code, _, _ = _get(url)
    if code != 200:
        print("[bench] %s 不可用（HTTP %s），VPNTray 没在跑？" % (url, code), file=sys.stderr)
        return 2

    samples = []
    sizes = []
    for _ in range(max(3, a.n)):
        st, ms, body = _get(url)
        samples.append(ms)
        sizes.append(len(body))

    samples.sort()
    n = len(samples)
    p50 = statistics.median(samples)
    p95 = samples[min(n - 1, int(n * 0.95))]
    mx = samples[-1]
    nb = sizes[-1]
    bad = p95 > a.max_ms or nb > a.max_kb * 1024
    res = {"url": url, "n": n, "p50_ms": round(p50, 2), "p95_ms": round(p95, 2),
           "max_ms": round(mx, 2), "bytes": nb, "budget_p95_ms": a.max_ms,
           "budget_bytes": int(a.max_kb * 1024), "ok": not bad}

    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print("GET /api/state  n=%d  p50=%.1fms  p95=%.1fms  max=%.1fms  body=%dB"
              % (n, p50, p95, mx, nb))
        print("预算：p95 <= %.0fms，body <= %.0fKB  ->  %s"
              % (a.max_ms, a.max_kb, "OK" if not bad else "超预算"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())