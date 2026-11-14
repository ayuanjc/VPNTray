#!/usr/bin/env python3
"""安装体积预算门禁（见 docs/技术设计-性能与体积.md 第 2 节）。

用法：
    python -X utf8 tools/disk_budget.py E:/VPNTray-2.5.7
    python -X utf8 tools/disk_budget.py release/VPNTray-1.0.2 --json

退出码：0 = 达预算；1 = 超预算；2 = 目录不存在。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

MB = 1024.0 * 1024.0

# 目录 -> MB 上限（None = 不单独约束）
DEFAULT_BUDGET = {
    "": 165.0,
    "_internal": 80.0,
    "bin": 90.0,
    "data": 14.0,
}


def size_of(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    root = os.path.abspath(a.dir)
    if not os.path.isdir(root):
        print("[disk] 目录不存在：%s" % root, file=sys.stderr)
        return 2

    rows = []
    bad = False
    for sub, cap in DEFAULT_BUDGET.items():
        p = os.path.join(root, sub) if sub else root
        if not os.path.isdir(p):
            continue
        mb = size_of(p) / MB
        over = cap is not None and mb > cap
        bad = bad or over
        rows.append({"dir": sub or ".", "mb": round(mb, 1), "cap": cap, "over": over})

    # 顺手报三个最大的子目录，超了知道往哪看
    tops = []
    if os.path.isdir(os.path.join(root, "_internal")):
        ip = os.path.join(root, "_internal")
        for name in os.listdir(ip):
            fp = os.path.join(ip, name)
            tops.append((size_of(fp) / MB if os.path.isdir(fp) else os.path.getsize(fp) / MB, name))
        tops.sort(reverse=True)
        tops = tops[:5]

    if a.json:
        print(json.dumps({"root": root, "rows": rows,
                          "top_internal": [{"mb": round(m, 1), "name": n} for m, n in tops],
                          "ok": not bad}, ensure_ascii=False))
    else:
        for r in rows:
            mark = "超" if r["over"] else "ok"
            cap = "-" if r["cap"] is None else "%.0fMB" % r["cap"]
            print("%-12s %8.1fMB  (预算 %-6s)  %s" % (r["dir"], r["mb"], cap, mark))
        if tops:
            print("_internal 最大几项：" + "，".join("%s %.0fMB" % (n, m) for m, n in tops))
        print("-> %s" % ("达预算" if not bad else "超预算，见 docs/技术设计-性能与体积.md 第 3 节"))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())