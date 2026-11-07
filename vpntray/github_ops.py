"""GitHub 特化：五项链路自检 + git 全局代理对齐。

从 App 里抽出来的一块，只依赖 app 的 settings / state / api / _set 四个接口，
不碰内核生命周期、不改公开 API —— App 上原来的 github_check / git_probe /
sync_git_proxy 仍然是唯一对外入口（转发到这里）。

为什么值得单拎出来：本机历史上最难复现的两个故障都在这块 ——
「GitHub 组钉在死节点上，浏览器打不开而其他站全通」和「git 的 http.proxy
还指着上一次装的 Clash，git clone 报 unexpected eof」。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

import requests

from . import configgen
from .api import ApiError

# 和 app.py 共用同一个 logger 名字，日志才落在同一份 app.log 里
log = logging.getLogger("app")
CREATE_NO_WINDOW = 0x08000000

# 探针按「GitHub 实际会走到的域名」排：网页、API、raw 源文件、Release
# 资产、容器镜像。只 ping github.com 是不够的 —— 这台机器上就出现过
# 网页 200、raw 全 reset（hosts 固定 IP）的情况。
GITHUB_PROBES = (
    ("web", "网页", "https://github.com/"),
    ("api", "API", "https://api.github.com/"),
    ("raw", "raw 源文件", "https://raw.githubusercontent.com/git/git/master/README.md"),
    ("assets", "Release 资产", "https://objects.githubusercontent.com/"),
    ("ghcr", "容器镜像", "https://ghcr.io/v2/"),
)
# git 的代理只改「指向本机」的那种；公司网关之类的外部件一律不碰
GIT_PROXY_RE = re.compile(r"^(?:https?://)?(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?/?$", re.I)


def _mix_port(settings: Any) -> int:
    """混合端口（settings 里的脏值收敛成 1024~65535，否则回落 7891）。"""
    try:
        n = int(settings.get("mixed_port"))
    except (TypeError, ValueError):
        return 7891
    return n if 1024 <= n <= 65535 else 7891


class GithubOps:
    """一个 App 一个实例（App.__init__ 里建），无状态，状态都在 app 上。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    # ── git ──────────────────────────────────────────────
    def git_exe(self) -> str:
        return shutil.which("git") or ""

    def git_env(self, port: int) -> dict[str, str]:
        """git 子进程只走我们的代理，别继承 shell 里的坏代理变量。"""
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["GIT_TERMINAL_PROMPT"] = "0"     # 要账号就直接失败，别把线程挂住
        return env

    def git_probe(self, port: int | None = None) -> dict[str, Any]:
        """真的跑一次 git ls-remote —— 浏览器能开不等于 git 能拉。"""
        exe = self.git_exe()
        if not exe:
            return {"ok": False, "refs": 0, "error": "没装 git（PATH 里找不到）"}
        port = port or _mix_port(self.app.settings)
        px = f"http://127.0.0.1:{port}"
        cmd = [exe, "-c", f"http.proxy={px}", "-c", f"https.proxy={px}",
               "-c", "http.sslVerify=true",
               "ls-remote", "--heads", "https://github.com/git/git"]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                               env=self.git_env(port), encoding="utf-8",
                               errors="replace", creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            return {"ok": False, "refs": 0, "ms": 25000, "error": "git 25s 没拉完（链路太慢或断流）"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "refs": 0, "error": f"{type(e).__name__}: {e}"}
        out = (p.stdout or "").strip()
        refs = len([x for x in out.splitlines() if x.strip()])
        err = (p.stderr or "").strip().splitlines()
        return {
            "ok": p.returncode == 0 and refs > 0,
            "refs": refs,
            "ms": int((time.time() - t0) * 1000),
            "error": "" if p.returncode == 0 else (err[-1][:160] if err else f"exit {p.returncode}"),
        }

    # ── 五链探针 ─────────────────────────────────────────
    def probe_web(self, port: int) -> list[dict[str, Any]]:
        s = requests.Session()
        s.trust_env = False          # 别让系统 http_proxy 变量二次套娃
        px = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
        out: list[dict[str, Any]] = []
        for key, label, url in GITHUB_PROBES:
            t0 = time.time()
            code, err = 0, ""
            try:
                r = s.get(url, proxies=px, timeout=8, allow_redirects=False,
                          headers={"User-Agent": "VPNTray-github-check",
                                   "Cache-Control": "no-store"})
                code = int(r.status_code)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:140]
            ms = int((time.time() - t0) * 1000)
            # 401 / 403 / 404 也说明 TCP + TLS + HTTP 全程通了，只有 5xx 和
            # 异常才算这条链路坏 —— 用「必须 200」判会把 ghcr 永远判成失败。
            out.append({"key": key, "label": label, "url": url,
                        "ok": bool(code and code < 500), "code": code,
                        "ms": ms, "error": err})
        return out

    def check(self, heal: bool = True) -> dict[str, Any]:
        """GitHub 专项自检：探针 + git 实测，坏了就地换节点。

        这台机器上的典型故障是「GitHub 组钉在一个已经死掉的节点上」：
        浏览器打不开、git 报 unexpected eof，但内核本身活着、其他站都通。
        自检发现网页探针失败时，会用 GitHub 自己的地址给自动组重测一遍，
        把出口拉回健康节点，再复测一次 —— 用户点一下就好，不用手工试 100 个。
        """
        app = self.app
        port = _mix_port(app.settings)
        items = self.probe_web(port)
        bad = [i["key"] for i in items if not i["ok"]]
        switched = ""
        note = ""
        # 别用 state["proxies"]：那是刷新循环的缓存，可能落后好几秒，也可能还没跑过。
        # 自检要的是「此刻内核里到底有哪些组」，只能现查。
        try:
            live = app.api.proxies() or {}
            live_ok = bool(live)
        except Exception:  # noqa: BLE001
            live, live_ok = dict(app.state.get("proxies") or {}), False
        sc = configgen.scene_by_key("github")
        grp = (sc["title"] if sc else "") or "GitHub 加速"
        aname = configgen.auto_name(sc) if sc else ""
        if heal and bad:
            # 能查到组才谈得上换节点；只看 kernel.running() 会把「API 已死但进程还在」漏掉
            if not live_ok:
                note = "内核没响应，自动换节点这一步跳过"
            else:
                target = aname if (aname and aname in live) else grp
                try:
                    d = app.api.group_delay(
                        target, (sc or {}).get("probe") or "https://github.com/robots.txt", 3000)
                except Exception as e:  # noqa: BLE001
                    d = {}
                    log.info("GitHub 自检组测速异常: %s", e)
                if d:
                    best = min(d, key=lambda k: d[k])
                    # 有自动组就切回自动组（它自己会挑最快的），否则直接钉最快那个
                    want = aname if (aname and aname in live) else best
                    try:
                        app.api.select(grp, want)
                        switched = want if want != aname else f"{aname} → {best}"
                        note = f"已切到 {best}（{d[best]}ms）"
                    except ApiError as e:
                        note = f"切换失败：{e}"
                    if switched:
                        items = self.probe_web(port)
                        bad = [i["key"] for i in items if not i["ok"]]
                        if bad:
                            note += f"，换完仍有 {len(bad)} 项不通"
                        try:
                            live = app.api.proxies() or live
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    note = "自动组里没有能连上 github.com 的节点（机场中转段可能整片失效）"
        git = self.git_probe(port)
        res = {
            "ts": time.time(),
            "ok": not bad and git["ok"],
            "port": port,
            "items": items,
            "bad": bad,
            "git": git,
            "switched": switched,
            "note": note,
            "node": "",
        }
        g = (live.get(grp) or {})
        res["node"] = str(g.get("now") or "")
        app._set(github=res)
        log.info("GitHub 自检：%s，git %s，%s",
                 "通" if not bad else "断(" + ",".join(bad) + ")",
                 "ok" if git["ok"] else "fail", note or "未动")
        return res

    # ── git 代理对齐 ─────────────────────────────────────
    def sync_proxy(self) -> str:
        """把 git 全局代理对齐到当前混合端口，返回给人看的变更说明。

        典型症状：git 的 http.proxy 还指着上一次装的 Clash（127.0.0.1:7897），
        那个端口现在被 svchost / WSL 转发占着，连上去立刻 EOF —— 表现就是
        「GitHub 网页能开，git clone 报 unexpected eof while reading」。
        只改指向本机端口的配置；指向公司网关的外部件不动。
        """
        app = self.app
        if not bool(app.settings.get("git_proxy_sync", True)):
            return ""
        exe = self.git_exe()
        if not exe:
            return ""
        port = _mix_port(app.settings)
        want = f"http://127.0.0.1:{port}"
        changed: list[str] = []
        for key in ("http.proxy", "https.proxy"):
            try:
                cur = subprocess.run([exe, "config", "--global", "--get", key],
                                     capture_output=True, text=True, timeout=8,
                                     encoding="utf-8", errors="replace",
                                     creationflags=CREATE_NO_WINDOW).stdout.strip()
            except Exception as e:  # noqa: BLE001
                log.info("读 git %s 失败，跳过: %s", key, e)
                continue
            if not cur or cur.rstrip("/") == want:
                continue
            if not GIT_PROXY_RE.match(cur):
                log.info("git %s 指向外部代理 %s，不自动改", key, cur)
                continue
            try:
                subprocess.run([exe, "config", "--global", key, want],
                               capture_output=True, text=True, timeout=8,
                               creationflags=CREATE_NO_WINDOW)
            except Exception as e:  # noqa: BLE001
                log.info("git %s 改写失败: %s", key, e)
                continue
            changed.append(f"{key} {cur} → {want}")
        if changed:
            log.info("git 代理已对齐：%s", "；".join(changed))
        app._set(git_proxy=want, git_proxy_note="；".join(changed))
        return "；".join(changed)
