"""验证增量渲染真的生效：DOM 元素在轮询之间被复用，而不是整表重建。"""
import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    url = "http://127.0.0.1:7892/"
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(url, wait_until="networkidle")
        pg.wait_for_timeout(2500)

        before = pg.evaluate("""() => {
            const el = document.querySelector('#nodes .node');
            if (!el) return {ok: false};
            el.__mark = 'kept';
            return {ok: true, name: el.dataset.name,
                    total: document.querySelectorAll('#nodes .node').length};
        }""")
        print("标记首行:", before)

        pg.wait_for_timeout(4500)   # 至少跨 3 轮 1.5s 轮询

        after = pg.evaluate("""() => {
            const el = document.querySelector('#nodes .node');
            const marked = [...document.querySelectorAll('#nodes .node')]
                .filter(e => e.__mark === 'kept').length;
            return {nodeKept: !!el && marked > 0, marked,
                    total: document.querySelectorAll('#nodes .node').length,
                    seq: [...document.querySelectorAll('#nodes .node')]
                          .slice(0, 3).map(e => e.dataset.name)};
        }""")
        print("3 轮轮询后:", after)

        # 同一国家两次散点结果必须完全一致（原来用 Math.random，每帧跳位）
        det = pg.evaluate("""() => {
            const a = spreadSpots('JP', MAP_LIST).spots.map(s => s.join(',')).join('|');
            const b = spreadSpots('JP', MAP_LIST).spots.map(s => s.join(',')).join('|');
            return a === b && a.length > 0;
        }""")
        print("散点确定性:", det)

        # 全量重建一次后缓存要能自愈（切国家再切回来）
        pg.evaluate("() => pickCC('JP')")
        pg.wait_for_timeout(1500)
        pg.evaluate("() => pickCC('JP')")
        pg.wait_for_timeout(1500)
        healed = pg.evaluate("""() => ({
            nodes: document.querySelectorAll('#nodes .node').length,
            pins: document.querySelectorAll('#mapPins .pin').length,
        })""")
        print("切国家往返:", healed)
        print("JS 错误:", errs or "无")

        ok = (before.get("ok") and after.get("nodeKept") and det
              and healed["nodes"] > 0 and not errs)
        print("=== 结论:", "PASS" if ok else "FAIL", "===")
        b.close()
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
