"""验证「点国家面积即选中 / 点海洋取消」这条交互。

跑法: python tools/map_click_test.py
需要发布版或源码版在前台跑着（127.0.0.1:7892）。
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:7892/"
OUT = Path(__file__).resolve().parent.parent / "data"


def find_hittable(page, cc: str):
    """在目标国家 bbox 里采点，返回第一个 elementFromPoint 真能命中它的屏幕坐标。"""
    return page.evaluate(
        """(cc) => {
            const p = document.querySelector(`.map-region[data-cc="${cc}"]`);
            if (!p) return null;
            const b = p.getBBox();
            const svg = document.getElementById('map');
            const ctm = svg.getScreenCTM();
            const pt = svg.createSVGPoint();
            for (let iy = 1; iy <= 4; iy++) {
              for (let ix = 1; ix <= 4; ix++) {
                pt.x = b.x + b.width * (ix / 5);
                pt.y = b.y + b.height * (iy / 5);
                const sp = pt.matrixTransform(ctm);
                const el = document.elementFromPoint(sp.x, sp.y);
                const reg = el && el.closest ? el.closest('.map-region') : null;
                if (reg && reg.dataset.cc === cc) {
                  return {x: sp.x, y: sp.y, w: b.width, h: b.height};
                }
              }
            }
            return {x: null, w: b.width, h: b.height};
        }""",
        cc,
    )


def ocean_point(page):
    return page.evaluate(
        """() => {
            const svg = document.getElementById('map');
            const pt = svg.createSVGPoint();
            pt.x = 300; pt.y = 330;      // 太平洋中部
            const sp = pt.matrixTransform(svg.getScreenCTM());
            return {x: sp.x, y: sp.y};
        }"""
    )


def map_hint(page) -> str:
    return page.evaluate("() => document.getElementById('mapHint').textContent.trim()")


def list_hint(page) -> str:
    return page.evaluate("() => document.getElementById('listHint').textContent.trim()")


def reset_view(page):
    page.evaluate("() => document.getElementById('zoomReset').click()")
    page.wait_for_timeout(450)


def main() -> int:
    results = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 1250})
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_function(
            "() => document.querySelectorAll('.map-region.has').length > 5", timeout=20000
        )
        pg.wait_for_timeout(1000)

        print("初始 地图提示:", map_hint(pg))
        print("初始 列表提示:", list_hint(pg))
        before = list_hint(pg)

        # ① 点澳大利亚面积（大地块）
        pt = find_hittable(pg, "AU")
        print(f"AU bbox {pt['w']:.0f}x{pt['h']:.0f} 命中点: {pt['x']}")
        pg.mouse.click(pt["x"], pt["y"])
        pg.wait_for_timeout(700)
        h1, l1 = map_hint(pg), list_hint(pg)
        print("① 点 AU 面积 →", h1, "||", l1)
        results["面积选中"] = ("已选中" in h1) and ("澳洲" in h1)
        pg.screenshot(path=str(OUT / "click_au.png"))

        # ② 点日本面积（细长小国）
        reset_view(pg)
        pt2 = find_hittable(pg, "JP")
        print(f"JP bbox {pt2['w']:.0f}x{pt2['h']:.0f} 命中点: {pt2['x']}")
        if pt2["x"] is not None:
            pg.mouse.click(pt2["x"], pt2["y"])
            pg.wait_for_timeout(700)
            h2, l2 = map_hint(pg), list_hint(pg)
            print("② 点 JP 面积 →", h2, "||", l2)
            results["小国可选"] = ("日本" in h2) and ("已选中" in h2)
        else:
            results["小国可选"] = False
            print("② 日本 bbox 内没有可命中的点（可能被 pin 完全盖住）")

        # ③ 点海洋取消（先把视角复位到全球，否则坐标已经不在视野里）
        pg.evaluate("() => document.getElementById('zoomReset').click()")
        pg.wait_for_timeout(500)
        op = ocean_point(pg)
        pg.mouse.click(op["x"], op["y"])
        pg.wait_for_timeout(700)
        h3, l3 = map_hint(pg), list_hint(pg)
        print("③ 点海洋 →", h3, "||", l3)
        results["海洋取消"] = ("已选中" not in h3) and (
            l3.split(" · ")[0] == before.split(" · ")[0]   # 后台还在测延迟，只比"N 个节点"
        )
        pg.screenshot(path=str(OUT / "click_ocean.png"))

        # ④ 点没有节点的国家（蒙古）
        pg.wait_for_timeout(300)
        pt4 = find_hittable(pg, "MN")
        if pt4["x"] is not None:
            pg.mouse.click(pt4["x"], pt4["y"])
            pg.wait_for_timeout(500)
            h4 = map_hint(pg)
            print("④ 点 MN（无节点）→", h4)
            results["无节点提示"] = "已选中" not in h4

        # ⑤ 拖拽不应误触筛选
        pg.evaluate("() => document.getElementById('zoomReset').click()")
        pg.wait_for_timeout(500)
        box = pg.evaluate(
            """() => { const r = document.getElementById('mapWrap').getBoundingClientRect();
                       return {x: r.x + r.width * 0.3, y: r.y + r.height * 0.4}; }"""
        )
        pg.mouse.move(box["x"], box["y"])
        pg.mouse.down()
        pg.mouse.move(box["x"] + 90, box["y"] + 50, steps=8)
        pg.mouse.up()
        pg.wait_for_timeout(600)
        h5 = map_hint(pg)
        print("⑤ 拖拽后 →", h5)
        results["拖拽不误触"] = "已选中" not in h5

        b.close()

    print("\n=== 结果 ===")
    for k, v in results.items():
        print(("  ✓ " if v else "  ✗ ") + k)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
