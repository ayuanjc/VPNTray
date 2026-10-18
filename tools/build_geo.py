"""把 Natural Earth 110m 国家边界转成内嵌 SVG 路径（assets/world_map.js）。

投影用 Miller 圆柱（比等距圆柱好看，不会把俄罗斯/加拿大拉得离谱），
坐标简化 + 相对路径，体积控制在 ~150KB。

用法: python tools/build_geo.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "tools" / "_geo_raw.json"
OUT = ROOT / "assets" / "world_map.js"

W = 1000.0
PAD = 6.0
TOLERANCE = 0.55  # 简化容差（像素）

# 这些地区在 ISO_A2 里是空值或有歧义，手动补
OVERRIDE = {
    "France": "FR", "Norway": "NO", "Somaliland": "", "Kosovo": "XK",
    "N. Cyprus": "", "Taiwan": "TW", "Hong Kong": "", "Macao": "",
}

# 110m 精度里没有图斑的国家/地区（太小或并入邻国），给个经纬度用于打点
MANUAL_LONLAT = {
    "HK": (114.17, 22.32),
    "SG": (103.82, 1.35),
    "MO": (113.54, 22.20),
    "MT": (14.51, 35.90),
    "BH": (50.58, 26.03),
    "LU": (6.13, 49.61),
    "CY": (33.43, 35.13),
}


def miller(lat: float) -> float:
    lat = max(-89.9, min(89.9, lat))
    return 1.25 * math.log(math.tan(math.pi / 4 + 0.4 * math.radians(lat)))


Y_TOP = miller(89.9)
H = W * (2 * Y_TOP) / (2 * math.pi) + PAD * 2


def project(lon: float, lat: float) -> tuple[float, float]:
    x = (lon + 180.0) / 360.0 * W + PAD
    y = (Y_TOP - miller(lat)) / (2 * Y_TOP) * (H - 2 * PAD) + PAD
    return x, y


def rdp(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Douglas–Peucker 折线简化。"""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1e-9
        best_i, best_d = -1, 0.0
        for i in range(a + 1, b):
            px, py = pts[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best_d:
                best_i, best_d = i, d
        if best_d > eps and best_i > 0:
            keep[best_i] = True
            stack.append((a, best_i))
            stack.append((best_i, b))
    return [p for p, k in zip(pts, keep) if k]


def rdp_closed(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """闭合环简化：首尾重合会让 RDP 的基线退化成一个点，必须先切成两段。"""
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 4:
        return pts
    x0, y0 = pts[0]
    far = max(range(1, len(pts)), key=lambda i: (pts[i][0] - x0) ** 2 + (pts[i][1] - y0) ** 2)
    a = rdp(pts[: far + 1], eps)
    b = rdp(pts[far:] + [pts[0]], eps)
    return a[:-1] + b[:-1]


def ring_to_path(ring: list[list[float]], eps: float) -> str:
    pts = [project(c[0], c[1]) for c in ring]
    if len(pts) > 4:
        pts = rdp_closed(pts, eps)
    if len(pts) < 3:
        return ""
    cmds = []
    px, py = pts[0]
    cmds.append(f"M{px:.0f} {py:.0f}")
    for x, y in pts[1:]:
        dx, dy = x - px, y - py
        px, py = x, y
        if abs(dx) < 1 and abs(dy) < 1:
            continue
        s = f"{dx:+.0f} {dy:+.0f}".replace("+", "")
        cmds.append("l" + s)
    if len(cmds) < 3:
        return ""
    return "".join(cmds) + "Z"


def main() -> int:
    if not RAW.exists():
        print(f"缺少数据文件: {RAW}", file=sys.stderr)
        return 1
    data = json.loads(RAW.read_text(encoding="utf-8"))

    paths: dict[str, str] = {}
    names: dict[str, str] = {}
    centers: dict[str, list[float]] = {}

    def bbox_center(poly: list[list[list[float]]]) -> tuple[float, float, float]:
        xs = [c[0] for ring in poly for c in ring]
        ys = [c[1] for ring in poly for c in ring]
        return min(xs), min(ys), max(xs), max(ys)

    for f in data["features"]:
        p = f["properties"]
        cc = (p.get("ISO_A2") or "").strip().upper()
        if cc in ("", "-99"):
            cc = OVERRIDE.get(p.get("ADMIN", ""), "") or OVERRIDE.get(p.get("NAME", ""), "")
        if cc and "-" in cc:  # CN-TW / AU-QLD 这类
            cc = cc.split("-")[-1]
        if not cc or cc == "-99":
            continue
        geom = f.get("geometry") or {}
        gtype = geom.get("type")
        polys: list[list[list[list[float]]]] = []
        if gtype == "Polygon":
            polys = [geom["coordinates"]]
        elif gtype == "MultiPolygon":
            polys = geom["coordinates"]
        else:
            continue

        segs = []
        best = (0.0, 0.0, 0.0, 0.0)
        best_area = -1.0
        for poly in polys:
            for ring in poly:  # 外环 + 内环（湖泊）
                seg = ring_to_path(ring, TOLERANCE)
                if seg:
                    segs.append(seg)
            try:
                x0, y0, x1, y1 = bbox_center(poly)
                area = (x1 - x0) * (y1 - y0)
                if area > best_area:
                    best_area, best = area, (x0, y0, x1, y1)
            except (ValueError, IndexError):
                pass

        if best_area > 0:
            cx, cy = project((best[0] + best[2]) / 2.0, (best[1] + best[3]) / 2.0)
            centers[cc] = [round(cx, 1), round(cy, 1)]

        zh = (p.get("NAME_ZH") or p.get("NAME") or cc).strip()
        if segs:
            path = "".join(segs)
            if cc in paths:  # 同一国家多块，合并
                paths[cc] += path
            else:
                paths[cc] = path
                names[cc] = zh
        else:
            names.setdefault(cc, zh)

    # 补上 110m 里没有图斑的小地区
    for cc, (lon, lat) in MANUAL_LONLAT.items():
        if cc not in centers:
            cx, cy = project(lon, lat)
            centers[cc] = [round(cx, 1), round(cy, 1)]

    # 面积太小的国家合并后仍保留（点选靠 title 提示）
    view_h = 588  # 裁掉南极（那里没有节点，白占屏幕）
    payload = (
        "/* 自动生成，勿手改：python tools/build_geo.py */\n"
        f"window.WORLD_VIEWBOX='0 0 {W + PAD * 2:.0f} {view_h}';\n"
        "window.WORLD_GEO=" + json.dumps(paths, ensure_ascii=False, separators=(",", ":")) + ";\n"
        "window.WORLD_NAMES=" + json.dumps(names, ensure_ascii=False, separators=(",", ":")) + ";\n"
        "window.WORLD_CENTER=" + json.dumps(centers, ensure_ascii=False, separators=(",", ":")) + ";\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(payload, encoding="utf-8")
    kb = OUT.stat().st_size / 1024
    print(f"国家数: {len(names)}  path: {len(paths)}  center: {len(centers)}")
    print(f"输出: {OUT}  ({kb:.0f} KB)")
    for cc in ("HK", "SG", "JP", "TW", "US", "KR", "MY", "PH", "MO"):
        print(f"  {cc} {names.get(cc, '?'):<6} path={len(paths.get(cc, '')):<5} center={centers.get(cc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
