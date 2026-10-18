"""端到端自检：订阅→配置→内核→默认节点→测速→切节点→国内/国外连通。

用法: python tools/e2e.py [--proxy]

默认不改系统代理（避免影响你正在用的代理软件）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests  # noqa: E402

from vpntray import logger  # noqa: E402
from vpntray.app import App  # noqa: E402

OK = "✓"
NO = "✗"


def probe(session: requests.Session, url: str, timeout: float = 15.0) -> tuple[int, float, str]:
    t0 = time.time()
    try:
        r = session.get(url, timeout=timeout, allow_redirects=False)
        return r.status_code, round(time.time() - t0, 2), (r.text.strip()[:80] if "ip" in url else "")
    except Exception as e:  # noqa: BLE001
        return 0, round(time.time() - t0, 2), f"{type(e).__name__}: {e}"


def main() -> int:
    logger.setup()
    use_proxy = "--proxy" in sys.argv
    app = App()
    orig_sysproxy = bool(app.settings.get("system_proxy", True))
    app.settings["system_proxy"] = use_proxy  # 只影响本次运行
    try:
        return _run(app, use_proxy)
    finally:
        # 别把自检用的临时设置写进用户配置
        app.settings["system_proxy"] = orig_sysproxy
        try:
            app.settings.save()
        except Exception:  # noqa: BLE001
            pass


def _run(app, use_proxy: bool) -> int:
    print("── 启动 ──")
    t0 = time.time()
    ok = app.start()
    print(f"{OK if ok else NO} 启动 {'成功' if ok else '失败'}，耗时 {time.time() - t0:.1f}s")
    if not ok:
        print("  错误:", app.state.get("error"))
        print("  订阅:", app.state.get("sub_error"))
        return 1

    st = app.snapshot()
    print(f"  主组: {st['main_group']}  当前: {st['group_now']}  节点数: {len(st['group_all'])}")
    print(f"  账户信息: {st.get('info_nodes')}")
    assert st["group_now"], "主组没有被选中"

    sess = requests.Session()
    sess.trust_env = False
    port = app.settings["mixed_port"]
    sess.proxies = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}

    print("── 测速（自动选择组）──")
    delays = app.test_delays("自动选择")
    best = dict(sorted(delays.items(), key=lambda kv: kv[1])[:5])
    print(f"  {len(delays)} 个节点可用；最快: {best}")
    assert delays, "没有任何节点测速通过"

    print("── 自动优选（测速 + 端到端验证）──")
    picked = app.auto_pick_best()
    print(f"  {OK if picked else NO} 选中: {picked}")
    assert picked, "找不到可用节点"

    print("── 连通性（当前=自动选择）──")
    for url in ("https://www.gstatic.com/generate_204", "https://www.youtube.com",
                "https://github.com", "https://chatgpt.com"):
        code, dt, _ = probe(sess, url, 20)
        print(f"  {OK if 200 <= code < 400 else NO} {url} -> {code} {dt}s")

    print("── 出口 IP ──")
    code, dt, ip = probe(sess, "https://api.ipify.org", 20)
    print(f"  {OK if code == 200 and ip else NO} {ip or code} ({dt}s)")

    print("── 国内直连校验 ──")
    direct_ip = requests.get("http://myip.ipip.net", timeout=10).text.strip()[:60]
    via_ip = probe(sess, "http://myip.ipip.net", 15)[2]
    print(f"  直连: {direct_ip}")
    print(f"  走代理端口: {via_ip}")
    print(f"  {OK if via_ip and via_ip == direct_ip else NO} 国内站点经代理端口仍走本地出口（=规则分流生效）")
    for url in ("https://www.baidu.com", "https://www.taobao.com", "https://www.bilibili.com"):
        code, dt, _ = probe(sess, url, 15)
        print(f"  {OK if code == 200 else NO} {url} -> {code} {dt}s")

    print("── 切换节点 ──")
    target = next((n for n in app.state["group_all"] if "新加坡" in n and "IEPL" in n), None)
    target = target or next(n for n in app.state["group_all"] if n not in ("DIRECT", "自动选择"))
    app.select_node(target)
    time.sleep(1.5)
    before = probe(sess, "https://api.ipify.org", 20)[2]
    now = app.snapshot()["group_now"]
    print(f"  {OK if now == target else NO} 已切到 {now}")
    print(f"  切换后出口: {before}")

    print("── 速度/连接统计 ──")
    probe(sess, "https://speed.cloudflare.com/__down?bytes=2000000", 30)
    time.sleep(2.5)
    app.refresh_now()
    t = app.snapshot()["traffic"]
    print(f"  {OK if t['down'] > 0 else NO} 累计 ↓{t['down']} B ↑{t['up']} B  连接 {app.snapshot()['connections']} 个")

    print("── 关闭 ──")
    app.shutdown()
    print(f"{OK} 已停止，系统代理未改动" if not use_proxy else f"{OK} 已停止，系统代理已还原")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
