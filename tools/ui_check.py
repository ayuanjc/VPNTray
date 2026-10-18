"""UI 冒烟：协议弹层 → 同意 → 代理冲突检测 → 反馈弹层 → AI 出口对照。

跑之前先启动 app（pythonw run.pyw），默认打 7892。
所有断言都是「页面上真的看到了」，不是「接口返回了」——
这两个经常不一致，而用户看到的是前者。
"""
from __future__ import annotations

import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = "http://127.0.0.1:7892/"
SHOT = "data/ui-check.png"


def api(path: str, body: dict | None = None):
    """body=None 走 GET（/api/state），否则 POST JSON。"""
    url = BASE.rstrip("/") + path
    if body is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> int:
    os_ver = api("/api/state").get("state", {})
    print("后端版本:", os_ver.get("ver"), "| agreed =", repr(os_ver.get("agreed")))

    errs: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        pg.on("console", lambda m: errs.append(f"console: {m.text}") if m.type == "error" else None)

        pg.goto(BASE, wait_until="networkidle")
        pg.wait_for_timeout(3500)

        # 1) 协议弹层必须出现（agreed 是空的）
        agree_on = pg.locator("#agreeModal").evaluate("el => el.classList.contains('on')")
        agree_text = pg.locator("#agreeModal .agree-body").inner_text()
        print(f"1) 协议弹层: {agree_on} | 正文 {len(agree_text)} 字")

        if agree_on:
            pg.screenshot(path="data/ui-agree.png", full_page=False)
            pg.click("#btnAgreeYes")
            pg.wait_for_timeout(1200)
        agree_after = pg.locator("#agreeModal").evaluate("el => el.classList.contains('on')")
        st = api("/api/state").get("state", {})
        print(f"   点同意后: 弹层={agree_after} | 后端 agreed={st.get('agreed')!r}")

        # 0) 卡片折叠可能是收起的（用户手动收过 / localStorage 里存着），先展开
        about_folded = pg.locator("#aboutCard").evaluate(
            "el => el.classList.contains('fold')")
        if about_folded:
            pg.click("#aboutCard > h2")
            pg.wait_for_timeout(300)
        print(f"0) 关于卡原始折叠态: {about_folded}（已确保展开）")

        # 2) 代理冲突检测
        pg.click("#btnNetGuard")
        pg.wait_for_timeout(4000)
        ng_hidden = pg.locator("#ngBox").evaluate("el => el.hidden")
        ng_text = pg.locator("#ngText").inner_text()
        print(f"2) 冲突面板: hidden={ng_hidden}")
        for line in ng_text.splitlines():
            print("   ", line)

        # 3) AI 出口对照
        split_on = pg.locator("#aiSplit").evaluate("el => el.classList.contains('on')")
        split_text = pg.locator("#aiSplit").inner_text() if split_on else "（未显示）"
        print(f"3) AI 出口对照: {split_on} → {split_text.replace(chr(10), ' / ')}")

        # 4) 反馈弹层
        pg.click("#btnFeedback")
        pg.wait_for_timeout(600)
        fb_on = pg.locator("#fbModal").evaluate("el => el.classList.contains('on')")
        print(f"4) 反馈弹层: {fb_on}")
        if fb_on:
            pg.fill("#fbTitle", "UI 冒烟测试")
            pg.fill("#fbDetail", "这是一条自动化测试生成的反馈，用来验证日志与脱敏。")
            pg.screenshot(path="data/ui-feedback.png", full_page=False)
            pg.click("#btnFbLocal")
            pg.wait_for_timeout(2500)
            res = pg.locator("#fbResult").inner_text()
            print(f"   生成结果: {res.replace(chr(10), ' | ')}")

        pg.screenshot(path=SHOT, full_page=True)
        b.close()

    print("JS 错误:", errs if errs else "无")

    # 检查生成的反馈文件是否真的脱敏了
    from pathlib import Path
    fb_dir = Path("data/feedback")
    if fb_dir.exists():
        files = sorted(fb_dir.glob("*.md"))
        if files:
            txt = files[-1].read_text(encoding="utf-8")
            leak = [k for k in ("token=", "cat.cn-ping.com/sabusuku") if k in txt]
            print(f"5) 反馈文件 {files[-1].name}: {len(txt)} 字 | 疑似泄露: {leak or '无'}")

    return 0 if not errs else 1


if __name__ == "__main__":
    sys.exit(main())
