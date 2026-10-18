"""港澳局部地图交互验证：点香港 → 局部图 → 点编号切节点 → 返回世界地图。"""
import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:7892/"


def main() -> int:
    res = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 1250})
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_function(
            "() => document.querySelectorAll('.map-region.has').length > 5", timeout=25000
        )
        pg.wait_for_timeout(800)

        # ① 点香港面积 → 局部地图
        pg.evaluate(
            "() => document.querySelector('.map-region[data-cc=HK]')"
            " ? document.querySelector('.map-region[data-cc=HK]').dispatchEvent(new MouseEvent('click',{bubbles:true}))"
            " : null"
        )
        # 香港没有独立轮廓，走 JS 直接选
        pg.evaluate("() => window.LOCAL_MAPS && pickCC('HK')")
        pg.wait_for_timeout(1200)
        info = pg.evaluate(
            """() => {
                const lm = document.getElementById('localMap');
                const back = document.getElementById('localBack');
                const world = document.getElementById('map');
                return {
                    localShown: getComputedStyle(lm).display !== 'none',
                    worldHidden: getComputedStyle(world).display === 'none',
                    backShown: getComputedStyle(back).display !== 'none',
                    regions: document.querySelectorAll('#localRegions .lregion').length,
                    pins: document.querySelectorAll('#mapPins .pin.num').length,
                    vb: lm.getAttribute('viewBox'),
                };
            }"""
        )
        print("① 局部地图:", info)
        res["香港局部图"] = (
            info["localShown"] and info["worldHidden"] and info["backShown"]
            and info["regions"] >= 18 and info["pins"] >= 5
        )

        # ② pin 是否都落在可视区内
        inview = pg.evaluate(
            """() => {
                const ps=[...document.querySelectorAll('#mapPins .pin.num')];
                return ps.filter(p => p.getBoundingClientRect().width > 0).length;
            }"""
        )
        print(f"② 可见编号点: {inview} / {info['pins']}")
        res["编号点可见"] = inview >= max(3, info["pins"] - 2)

        # ③ 点编号切节点
        before = pg.evaluate(
            "() => ((STATE.nodes||[]).find(x=>x.current)||{}).name || ''"
        )
        target = pg.evaluate(
            """(before) => {
                const ps=[...document.querySelectorAll('#mapPins .pin.num')]
                  .filter(p => p.getBoundingClientRect().width > 0);
                const t = ps.find(p => p.dataset.node !== before);
                if (!t) return null;
                const r = t.getBoundingClientRect();
                return {node: t.dataset.node, x: r.x + r.width/2, y: r.y + r.height/2};
            }""",
            before,
        )
        if target:
            pg.mouse.click(target["x"], target["y"])
            ok = False
            for _ in range(20):
                pg.wait_for_timeout(1000)
                now = pg.evaluate(
                    "() => ((STATE.nodes||[]).find(x=>x.current)||{}).name || ''"
                )
                if now == target["node"]:
                    ok = True
                    break
            print(f"③ 点编号切换: {before} → {target['node']} (ok={ok})")
            res["点编号切换"] = ok
        else:
            print("③ 没有可点的编号")
            res["点编号切换"] = False

        # ④ 返回世界地图
        pg.evaluate("() => document.getElementById('localBack').click()")
        pg.wait_for_timeout(1000)
        back = pg.evaluate(
            """() => ({
                localHidden: getComputedStyle(document.getElementById('localMap')).display === 'none',
                worldShown: getComputedStyle(document.getElementById('map')).display !== 'none',
                filter: window.MAP_FILTER !== undefined ? MAP_FILTER : null,
                hint: document.getElementById('mapHint').textContent,
            })"""
        )
        print("④ 返回:", back)
        res["返回世界地图"] = back["localHidden"] and back["worldShown"]

        # ⑤ 澳门
        pg.evaluate("() => pickCC('MO')")
        pg.wait_for_timeout(1200)
        mo = pg.evaluate(
            """() => ({
                localShown: getComputedStyle(document.getElementById('localMap')).display !== 'none',
                regions: document.querySelectorAll('#localRegions .lregion').length,
                pins: document.querySelectorAll('#mapPins .pin.num').length,
            })"""
        )
        print("⑤ 澳门:", mo)
        res["澳门局部图"] = mo["localShown"] and mo["regions"] >= 8

        b.close()

    print("\n=== 结果 ===")
    for k, v in res.items():
        print(f"  {'✓' if v else '✗'} {k}")
    return 0 if all(res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
