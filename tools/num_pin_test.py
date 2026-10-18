"""验证「选中国家 → 放大 → 逐节点编号落点 → 点编号切节点」。

跑法: python tools/num_pin_test.py
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:7892/"
OUT = Path(__file__).resolve().parent.parent / "data"


def pins(pg):
    return pg.evaluate(
        """() => [...document.querySelectorAll('#mapPins .pin.num')].map(p => {
            const r = p.getBoundingClientRect();
            return {
                idx: p.textContent.trim(),
                node: p.dataset.node,
                cls: p.className,
                vis: r.width > 0 && r.height > 0,   // 落地在境外的点会被移出视野
                x: r.x + r.width / 2,
                y: r.y + r.height / 2,
            };
        })"""
    )


def cur_node(pg):
    return pg.evaluate("() => (STATE.nodes || []).find(n => n.current)?.name || ''")


def main() -> int:
    res = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 1250})
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_function(
            "() => document.querySelectorAll('.map-region.has').length > 5", timeout=20000
        )
        pg.wait_for_timeout(1200)

        # ① 点日本面积 → 出现编号 pin，数量与列表一致
        pg.evaluate("() => { const r = document.querySelector('.map-region[data-cc=\"JP\"]');"
                    "r.dispatchEvent(new MouseEvent('click', {bubbles:true})); }")
        pg.wait_for_timeout(900)
        ps = pins(pg)
        listed = pg.evaluate("() => document.querySelectorAll('#nodes .node .idx').length")
        print(f"① 日本：编号点 {len(ps)} 个，列表编号 {listed} 个")
        print("   编号:", ",".join(x["idx"] for x in ps))
        res["编号出现"] = len(ps) == 11 and listed == 11
        res["编号对齐列表"] = [x["idx"] for x in ps] == [str(i) for i in range(1, len(ps) + 1)]

        # ② 编号点都落在该国陆地上（局部地图用 #localRegions 的区界判定）
        inside = pg.evaluate(
            """() => {
                const svg = document.getElementById('localMap');
                const ctm = svg.getScreenCTM().inverse();
                const pt = svg.createSVGPoint();
                const shapes = [...document.querySelectorAll('#localRegions .lregion')];
                const world = document.getElementById('map');
                const wctm = world.getScreenCTM().inverse();
                const wpt = world.createSVGPoint();
                const jp = document.querySelector('.map-region[data-cc="JP"]');
                let ok = 0, tot = 0, local = getComputedStyle(svg).display !== 'none';
                document.querySelectorAll('#mapPins .pin.num').forEach(el => {
                    tot++;
                    const r = el.getBoundingClientRect();
                    if (local) {
                        pt.x = r.x + r.width / 2; pt.y = r.y + r.height / 2;
                        const loc = pt.matrixTransform(ctm);
                        const hit = shapes.some(s => {
                            try { return s.isPointInFill(new DOMPoint(loc.x, loc.y)); } catch (e) { return false; }
                        });
                        if (hit) ok++;
                    } else if (jp) {
                        wpt.x = r.x + r.width / 2; wpt.y = r.y + r.height / 2;
                        const loc = wpt.matrixTransform(wctm);
                        if (jp.isPointInFill(new DOMPoint(loc.x, loc.y))) ok++;
                    }
                });
                return {ok, tot, local};
            }"""
        )
        print(f"② 落在陆地上的点: {inside['ok']}/{inside['tot']}（局部地图: {inside['local']}）")
        res["落在国土内"] = inside["ok"] >= int(inside["tot"] * 0.7)

        # ③ 点一个「不是当前节点」的编号 → 切过去
        before = cur_node(pg)
        target = next((x for x in ps if x["vis"] and x["node"] != before), None)
        if not target:
            print("③ 没有可点的编号（都不可见或都是当前节点）")
            res["点编号切节点"] = False
        else:
            pg.mouse.click(target["x"], target["y"])
            ok = False
            for _ in range(20):          # 切节点要断开重连，轮询 20 秒
                pg.wait_for_timeout(1000)
                if cur_node(pg) == target["node"]:
                    ok = True
                    break
            after = cur_node(pg)
            print(f"③ 点编号{target['idx']}（{target['node']}）: {before} → {after}")
            res["点编号切节点"] = ok and after == target["node"] and after != before

        # ④ 点「返回世界地图」取消 → 回到聚簇 pin
        has_back = pg.evaluate(
            "() => getComputedStyle(document.getElementById('localBack')).display !== 'none'"
        )
        if has_back:
            pg.click('#localBack')
            print("④ 用「返回世界地图」取消")
        else:
            pg.evaluate("() => document.getElementById('zoomReset').click()")
            pg.wait_for_timeout(600)
            op = pg.evaluate(
                """() => { const svg = document.getElementById('map');
                    const pt = svg.createSVGPoint(); pt.x = 300; pt.y = 330;
                    const sp = pt.matrixTransform(svg.getScreenCTM());
                    return {x: sp.x, y: sp.y}; }"""
            )
            pg.mouse.click(op["x"], op["y"])
            print("④ 点海洋取消")
        pg.wait_for_timeout(800)
        n_num = len(pins(pg))
        n_all = pg.evaluate("() => document.querySelectorAll('#mapPins .pin').length")
        print(f"④ 取消后：编号点 {n_num} 个 / 总 pin {n_all} 个")
        res["取消后还原"] = n_num == 0 and n_all > 0

        pg.screenshot(path=str(OUT / "num_after.png"))
        b.close()

    print("\n=== 结果 ===")
    for k, v in res.items():
        print(("  ✓ " if v else "  ✗ ") + k)
    return 0 if all(res.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
