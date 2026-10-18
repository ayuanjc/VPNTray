"""把「有节点的国家」都编译成局部地图 SVG，供前端点国家时打开。

两个数据源：
  · 港澳：阿里 DataV 区级边界（810000_full / 820000_full），能看到 18 区 / 8 堂区
  · 其它：Natural Earth 10m admin-0（ne_10m_admin_0_countries.geojson），国界轮廓

投影：等距圆柱 + 纬度余弦压缩。范围只有几千公里，误差可忽略。
飞地处理：像美国（阿拉斯加/夏威夷）、法国（法属圭亚那）这种经度跨度特别大的国家，
只保留离主质心近的部分，否则主体会被压成一条缝。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEO = ROOT / "tools" / "_geo"
OUT = ROOT / "assets" / "local_map.js"

WIDTH = 1000.0
PAD = 14.0
TARGET_PTS = 1100          # 每个国家简化后的目标点数
MAX_LON_SPAN = 90.0        # 超过这个经度跨度就裁掉远处的飞地
FAR_LIMIT = 45.0           # 离主质心超过这么多经度就丢

DISTRICT_MAPS = {"HK": "810000", "MO": "820000"}   # 港澳走区级数据


NAME_FIX = {"TW": "台湾", "KR": "韩国", "US": "美国", "GB": "英国", "AE": "阿联酋"}


def rings(geom: dict) -> list[list[tuple[float, float]]]:
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        return []
    out = []
    for poly in polys:
        for ring in poly:
            pts = [(float(p[0]), float(p[1])) for p in ring]
            if len(pts) >= 4:
                out.append(pts)
    return out


def _rdp_open(pts, eps):
    if len(pts) < 3:
        return pts
    x1, y1 = pts[0]
    x2, y2 = pts[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy)
    idx, best = 0, -1.0
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        d = math.hypot(px - x1, py - y1) if norm == 0 else abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
        if d > best:
            best, idx = d, i
    if best <= eps:
        return [pts[0], pts[-1]]
    return _rdp_open(pts[: idx + 1], eps)[:-1] + _rdp_open(pts[idx:], eps)


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    closed = pts[0] == pts[-1]
    body = pts[:-1] if closed else pts
    if len(body) < 3:
        return pts
    if closed:
        far = max(range(len(body)),
                  key=lambda i: (body[i][0] - body[0][0]) ** 2 + (body[i][1] - body[0][1]) ** 2)
        a = _rdp_open(body[: far + 1], eps)
        b = _rdp_open(body[far:] + [body[0]], eps)
        return a[:-1] + b[:-1] + [a[0]]
    return _rdp_open(body, eps)


def poly_area(pts) -> float:
    s = 0.0
    for i in range(len(pts) - 1):
        s += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(s) / 2


def simplify_to(parts, total_target: int, eps_lo=0.0002, eps_hi=0.5):
    """二分 epsilon，让总点数接近目标（点数随 epsilon 单调下降）。"""
    n0 = sum(len(p) for p in parts)
    if n0 <= total_target:
        return parts
    lo, hi = eps_lo, eps_hi
    best = parts
    for _ in range(22):
        mid = (lo + hi) / 2
        cur = [rdp(p, mid) for p in parts]
        cur = [c for c in cur if len(c) >= 4]
        n = sum(len(c) for c in cur)
        if n > total_target:
            lo = mid
        else:
            hi = mid
            best = cur
        if abs(n - total_target) < total_target * 0.08:
            best = cur
            break
    return best


def make_map(cc: str, name: str, parts, districts=None) -> dict | None:
    if not parts:
        return None
    lons = [p[0] for r in parts for p in r]
    lats = [p[1] for r in parts for p in r]

    # 飞地裁剪：跨度过大时只留主质心附近
    if max(lons) - min(lons) > MAX_LON_SPAN:
        big = max(parts, key=poly_area)
        cx = sum(p[0] for p in big) / len(big)
        cy = sum(p[1] for p in big) / len(big)
        keep = [r for r in parts
                if abs(sum(p[0] for p in r) / len(r) - cx) <= FAR_LIMIT
                and abs(sum(p[1] for p in r) / len(r) - cy) <= FAR_LIMIT * 0.5]
        if keep:
            parts = keep
            lons = [p[0] for r in parts for p in r]
            lats = [p[1] for r in parts for p in r]

    lon0, lon1 = min(lons), max(lons)
    lat0, lat1 = min(lats), max(lats)
    kx = math.cos(math.radians((lat0 + lat1) / 2))
    span_x = (lon1 - lon0) * kx
    span_y = lat1 - lat0
    if span_x <= 0 or span_y <= 0:
        return None
    inner = WIDTH - PAD * 2
    scale = inner / max(span_x, span_y)
    off_x = (WIDTH - span_x * scale) / 2
    height = span_y * scale + PAD * 2
    off_y = PAD

    def proj(lon, lat):
        return (off_x + (lon - lon0) * kx * scale, off_y + (lat1 - lat) * scale)

    def to_path(rs):
        out = []
        for r in rs:
            d = ""
            for i, (lon, lat) in enumerate(r):
                x, y = proj(lon, lat)
                d += ("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}"
            out.append(d + "Z")
        return "".join(out)

    if districts:
        items = [{"name": n, "d": to_path(rs)} for n, rs in districts if rs]
    else:
        items = [{"name": name, "d": to_path(parts)}]

    npts = sum(len(r) for r in parts)
    print(f"  {cc} {name}: {npts} 点 / {len(items)} 块 / {WIDTH:.0f}x{height:.0f}")
    return {
        "cc": cc,
        "name": name,
        "w": round(WIDTH, 2),
        "h": round(height, 2),
        "lon0": round(lon0, 5),
        "latMax": round(lat1, 5),
        "kx": round(kx, 6),
        "scale": round(scale, 3),
        "offX": round(off_x, 2),
        "offY": round(off_y, 2),
        "districts": items,
    }


def main() -> int:
    ccs = sys.argv[1:] or []
    if not ccs:
        # 默认：当前节点表里出现过的国家
        ccs = ["HK", "JP", "US", "TW", "SG", "KR", "VN", "DE", "GB", "FR", "IT", "NO",
               "CA", "AU", "UA", "TR", "AE", "NG", "PH", "TH", "MY", "IN", "ID", "AR",
               "BR", "MO"]

    out = {}

    # 港澳：区级
    for cc, code in DISTRICT_MAPS.items():
        if cc not in ccs:
            continue
        src = GEO / f"{cc}_full.json"
        if not src.exists():
            print(f"跳过 {cc}：缺少 {src}")
            continue
        d = json.loads(src.read_text(encoding="utf-8"))
        items = []
        allparts = []
        for f in d.get("features") or []:
            nm = (f.get("properties") or {}).get("name") or ""
            rs = [rdp(r, 0.00015) for r in rings(f["geometry"])]
            rs = [r for r in rs if len(r) >= 4]
            if rs:
                items.append((nm, rs))
                allparts.extend(rs)
        m = make_map(cc, {"HK": "香港", "MO": "澳门"}.get(cc, cc), allparts, items)
        if m:
            out[cc] = m

    # 其它国家：NE 10m
    ne = GEO / "ne10m.json"
    if ne.exists():
        data = json.loads(ne.read_text(encoding="utf-8"))
        by_cc = {}
        for f in data["features"]:
            p = f["properties"]
            for key in ("ISO_A2_EH", "ISO_A2"):
                c = (p.get(key) or "").upper()
                if c and c not in by_cc:
                    by_cc[c] = (p.get("NAME_ZH") or p.get("NAME") or c, f["geometry"])
        for cc in ccs:
            if cc in out or cc not in by_cc:
                continue
            name, geom = by_cc[cc]
            parts = rings(geom)
            if not parts:
                continue
            # 丢掉零星小岛（夏威夷、关岛、波多黎各…），否则 bbox 被撑到横跨半个地球
            areas = [poly_area(r) for r in parts]
            total_a = sum(areas)
            parts = [r for r, a in zip(parts, areas) if total_a and a / total_a >= 0.003]
            if not parts:
                continue
            parts = simplify_to(parts, TARGET_PTS)
            m = make_map(cc, NAME_FIX.get(cc, name), parts)
            if m:
                out[cc] = m
    else:
        print(f"缺少 {ne}，只有港澳")

    if not out:
        return 1
    js = "// 由 tools/build_local.py 生成，勿手改\nwindow.LOCAL_MAPS = " + json.dumps(
        out, ensure_ascii=False, separators=(",", ":")
    ) + ";\n"
    OUT.write_text(js, encoding="utf-8")
    print(f"\n写出 {OUT} ({OUT.stat().st_size / 1024:.0f} KB) / {len(out)} 个国家地区")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
