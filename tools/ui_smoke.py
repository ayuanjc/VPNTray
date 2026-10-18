"""控制台交互冒烟：抓 JS 报错，跑一遍主要交互。

用法：python -X utf8 tools/ui_smoke.py
前提：程序已在跑（127.0.0.1:7892）。
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:7892/"


def main() -> int:
    # 节点名带国旗 emoji，Windows 控制台默认 GBK，print 会直接抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    errs: list[tuple[str, str]] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        pg.on("console", lambda m: errs.append((m.type, m.text)) if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: errs.append(("pageerror", str(e))))

        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(3500)
        print("标题版本:", pg.text_content("#uiVer"))
        print("状态:", pg.text_content("#statText"))
        print("顶部横幅:", repr(pg.text_content("#banner")))
        print("节点行数:", pg.eval_on_selector_all("#nodes .node", "e=>e.length"))
        print("订阅条目:", pg.eval_on_selector_all("#subList .sub-item .nm", "e=>e.map(x=>x.textContent)"))
        print("流量:", pg.text_content("#sbTraffic"))

        pg.click('#mapView button[data-v="topo"]')
        pg.wait_for_timeout(2600)
        print("拓扑提示:", pg.text_content("#mapHint")[:70])
        pg.click('#mapView button[data-v="ppl"]')
        pg.wait_for_timeout(1400)

        pg.evaluate("pickCC('JP')")
        pg.wait_for_timeout(2000)
        print("选日本:", pg.text_content("#mapHint")[:70])
        print("编号点:", pg.eval_on_selector_all("#mapPins .pin.num", "e=>e.length"))
        pg.evaluate("pickCC(null)")
        pg.wait_for_timeout(1200)

        pg.click('#mapMode button[data-m="purity"]')
        pg.wait_for_timeout(1500)
        pg.click('#mapMode button[data-m="delay"]')
        pg.wait_for_timeout(1200)

        pg.click("#btnLog")
        pg.wait_for_timeout(900)
        print("日志弹层:", pg.eval_on_selector("#modal", "e=>e.classList.contains('on')"))
        pg.click("#btnCloseLog")
        pg.wait_for_timeout(500)
        b.close()

    print("\n=== JS 错误 ===")
    if errs:
        for k, v in errs:
            print(f"  [{k}] {v}")
        return 1
    print("  无")
    return 0


if __name__ == "__main__":
    sys.exit(main())
