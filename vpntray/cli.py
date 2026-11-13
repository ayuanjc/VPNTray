"""无 GUI 的命令行入口，便于调试与脚本化控制。

用法:
    python -m vpntray.cli up [--no-proxy]   前台拉起内核（调试用）
    python -m vpntray.cli status           查看当前状态
    python -m vpntray.cli nodes            列出节点及延迟
    python -m vpntray.cli set <节点名>      切换节点
    python -m vpntray.cli best             测速并切到最快节点
    python -m vpntray.cli mode <rule|global|direct>
    python -m vpntray.cli sub              强制更新订阅
    python -m vpntray.cli proxy <on|off>   系统代理开关
    python -m vpntray.cli down [--yes]     停止内核并还原系统代理（不加 --yes 会先确认）
"""
from __future__ import annotations

import argparse
import sys
import time

from . import configgen, logger, subscription, sysproxy
from .api import MihomoAPI
from .app import App
from .kernel import Kernel
from .paths import CONFIG
from .settings import Settings, port_setting


def _out(*a) -> None:
    print(*a)


def _safe_int(value, default: int) -> int:
    """把设置里的数字安全转成 int（手改过的 settings.json 什么都写得出来）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main(argv: list[str] | None = None) -> int:
    """命令行总入口：任何异常都翻成一句中文提示 + 非零退出码。

    命令行是最容易拿来「看一眼到底怎么了」的地方，甩一屏 traceback 对用户
    没有信息量；完整堆栈照样进日志文件，需要排查时去看 app.log。
    """
    try:
        return _main(argv)
    except SystemExit:
        raise                      # argparse 的 --help / 参数错误自带退出码
    except KeyboardInterrupt:
        _out("\n已取消")
        return 130
    except Exception as e:  # noqa: BLE001
        _out(f"执行失败：{type(e).__name__}: {e}")
        logger.get("cli").exception("命令行执行失败")
        return 1


def _main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog="vpntray")
    p.add_argument("cmd", nargs="?", default="status")
    p.add_argument("arg", nargs="?")
    p.add_argument("--no-proxy", action="store_true", help="不接管系统代理")
    p.add_argument("--yes", "-y", action="store_true",
                   help="down 时不再询问，直接停内核并还原系统代理")
    args = p.parse_args(argv)

    logger.setup()
    s = Settings()
    # 手改坏的 controller_port 会让每个请求都连到不存在的端口，一律走安全取值
    api = MihomoAPI(port=port_setting(s, "controller_port", 9091),
                    secret=s.get("secret") or "")

    if args.cmd == "up":
        app = App()
        if args.no_proxy:
            app.settings["system_proxy"] = False  # 只本次生效，不写盘
        ok = app.start()
        _out("已启动，Ctrl+C 退出" if ok else f"启动失败: {app.state.get('error')}")
        if not ok:
            return 1
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            _out("\n正在退出…")
            app.shutdown()
        return 0

    if args.cmd == "down":
        # 还原系统代理 + 杀内核都是立刻能看见后果的动作：命令行里一个手滑的
        # down 就会把用户正在用的代理断掉，所以缺省只打印计划并要求确认
        mixed = port_setting(s, "mixed_port", 7891)
        if not args.yes:
            _out("将执行以下动作：")
            _out(f"  1) 关闭 Windows 系统代理（当前指向 127.0.0.1:{mixed} 时会被还原）")
            _out(f"  2) 结束监听端口 {mixed} 的 mihomo 内核进程")
            stdin = sys.stdin
            if stdin is None or not stdin.isatty():
                # 脚本里误调 down 时绝不能替用户「猜」成同意
                _out("非交互环境，已取消。确认执行请加 --yes："
                     "python -m vpntray.cli down --yes")
                return 2
            try:
                ans = input("确认执行？输入 yes 继续：").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _out("\n已取消")
                return 2
            if ans not in ("yes", "y"):
                _out("已取消")
                return 2
        sysproxy.disable()
        pid = _kill_by_port(mixed)
        _out(f"已停止（内核 pid={pid}）" if pid else "没有发现正在监听的内核")
        return 0

    # 其余命令里只有这些依赖运行中的内核
    if args.cmd in {"status", "nodes", "set", "best", "mode"} and not api.alive():
        _out("内核未运行。先执行: python -m vpntray.cli up")
        return 2

    if args.cmd == "status":
        v = api.version()
        cfg = api.configs()
        proxies = api.proxies()
        group = s.get("main_group", "节点选择")
        g = proxies.get(group, {})
        c = api.connections()
        _out(f"内核: {v.get('version')} {v.get('meta')}")
        _out(f"模式: {cfg.get('mode')}   端口: {cfg.get('mixed-port')}")
        _out(f"主组: {group} -> {g.get('now')}")
        _out(f"节点数: {len(g.get('all') or [])}")
        _out(f"累计: ↑{c.get('uploadTotal', 0)} ↓{c.get('downloadTotal', 0)}  连接 {len(c.get('connections') or [])}")
        server = f"127.0.0.1:{port_setting(s, 'mixed_port', 7891)}"
        _out(f"系统代理: {'开' if sysproxy.is_enabled_to(server) else '关'}  ({server})")
        return 0

    if args.cmd == "nodes":
        group = s.get("main_group", "节点选择")
        proxies = api.proxies()
        g = proxies.get(group, {})
        delays = api.group_delay(group, s.get("delay_url"),
                                _safe_int(s.get("delay_timeout_ms"), 5000))
        now = g.get("now")
        for n in g.get("all") or []:
            mark = "●" if n == now else " "
            d = f"{delays[n]}ms" if n in delays else "  -  "
            _out(f"{mark} {d:>8}  {n}")
        return 0

    if args.cmd == "set":
        if not args.arg:
            _out("需要节点名")
            return 2
        group = s.get("main_group", "节点选择")
        try:
            api.select(group, args.arg)
        except api.ApiError as e:
            _out(f"切换失败: {e}")
            return 1
        _out(f"{group} -> {args.arg}")
        return 0

    if args.cmd == "best":
        app = App()
        d = app.test_delays()
        best = app.auto_pick_best()
        _out(f"最快: {best} {d.get(best)}ms" if best else "测速无结果")
        return 0

    if args.cmd == "mode":
        m = args.arg or "rule"
        try:
            api.set_mode(m)
        except api.ApiError as e:
            _out(f"设置模式失败: {e}")
            return 1
        s["mode"] = m
        s.save()
        _out(f"mode -> {m}")
        return 0

    if args.cmd == "sub":
        app = App()
        ok = app.update_subscription(force=True)
        sub = subscription.load_cache()
        if ok and sub:
            cfg = configgen.build(subscription.normalize(sub), s)
            configgen.write(cfg)
            _out(f"订阅已更新: {len(sub['proxies'])} 节点, {len(cfg['rules'])} 规则 -> {CONFIG}")
        else:
            _out("订阅更新失败")
            return 1
        return 0

    if args.cmd == "proxy":
        if (args.arg or "on") == "on":
            sysproxy.enable(f"127.0.0.1:{port_setting(s, 'mixed_port', 7891)}")
            _out("系统代理已开启")
        else:
            sysproxy.disable()
            _out("系统代理已关闭")
        return 0

    _out(__doc__)
    return 2


def _kill_by_port(port: int) -> int | None:
    """按端口找到监听进程并结束（用于停止没有托管的残留内核）。

    只杀 mihomo：端口有可能是用户自己的别的代理或开发服务，照着端口盲杀
    会误伤。进程名查不出来（tasklist 被拦截、进程刚退出、系统繁忙）时也绝不
    盲杀 —— 把 PID 和端口打出来，让用户自己确认。
    """
    import subprocess

    try:
        raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=8).stdout
        out = raw.decode("gbk", errors="replace")
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pid = line.split()[-1]
            if not pid.isdigit():
                continue
            try:
                raw2 = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, timeout=8,
                ).stdout
                name = raw2.decode("gbk", errors="replace").strip().split(",")[0].strip('"').lower()
            except Exception:  # noqa: BLE001
                name = ""
            if not name:
                # tasklist 失败时 name 是空串：以前会直接落到 taskkill，等于
                # 「不知道是谁就杀」，端口上可能是用户自己的服务
                _out(f"端口 {port} 的进程（PID {pid}）查不到名字，已跳过不杀。")
                _out(f"  确认是本程序残留内核后手工执行：taskkill /F /PID {pid}")
                return None
            if not name.startswith("mihomo"):
                _out(f"端口 {port} 被 {name}（PID {pid}）占用，不是本程序的内核，不处理")
                return None
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=8)
            except Exception:  # noqa: BLE001
                return None
            return int(pid)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
