# -*- coding: utf-8 -*-
"""本地面板的真实 HTTP 冒烟：起一个真 ThreadingHTTPServer，打真请求。

以前这份脚本的问题：端口写死 17892（占用就起不来）、w.stop() 不在 finally
（中途断言失败就漏线程）、modules.list 按 {"result": {...}} 取值而实际 payload
是摊在顶层的（于是"0 个模块"永远算通过，等于没测）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpntray import modules, webui  # noqa: E402

TOKEN = "smoke-token-0123456789abcdef0123456789"


class _S(dict):
    def save(self) -> None:
        pass


class _Stub:
    def __init__(self) -> None:
        self.settings = _S(ai_api_token=TOKEN, ai_api_enabled=True, ai_api_level="full",
                           ai_enabled=False, ai_node="", scene_auto=True,
                           mixed_port=7891, controller_port=9091, mode="rule")
        self.state = {}

    def modules_status(self):
        return [{"key": "demo", "title": "Demo", "kind": "scene", "enabled": True}]

    def module_get(self, key):
        return {"key": key, "title": "Demo"} if key == "demo" else None

    def module_save(self, payload):
        return (False, "stub")

    def module_delete(self, key):
        return (True, "")

    def module_reset(self):
        return (True, "")

    def module_export(self):
        return {"schema": 1, "modules": [{"key": "demo", "title": "Demo"}]}

    def module_import(self, payload):
        return (True, "")

    def modules_status_dict(self):
        return {}


def _req(base, path, data=None, headers=None):
    h = dict(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base + path, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def main() -> int:
    w = webui.WebUI(_Stub(), on_quit=lambda: None, port=0)
    if not w.start():
        print("FAIL 服务起不来")
        return 1
    base = "http://127.0.0.1:%d" % w.port
    out = {}
    bad = []
    try:
        out["no_token"] = _req(base, "/ai/v1/manifest")[0]
        out["bad_token"] = _req(base, "/ai/v1/manifest", headers={"X-AI-Token": "nope"})[0]
        st, hdr, body = _req(base, "/ai/v1/manifest", headers={"X-AI-Token": TOKEN})
        m = json.loads(body)
        out["manifest"] = (st, len(m.get("tools") or []), m.get("level"))
        out["bearer"] = _req(base, "/ai/v1/manifest",
                             headers={"Authorization": "Bearer " + TOKEN})[0]
        out["query_token"] = _req(base, "/ai/v1/manifest?token=" + TOKEN)[0]
        # 安全头：点击劫持 / MIME 嗅探
        out["xfo"] = hdr.get("X-Frame-Options")
        out["nosniff"] = hdr.get("X-Content-Type-Options")
        if out["xfo"] != "DENY":
            bad.append("缺 X-Frame-Options")
        if out["nosniff"] != "nosniff":
            bad.append("缺 X-Content-Type-Options")

        st, _, body = _req(base, "/ai/v1/call",
                           {"tool": "modules.list", "args": {}},
                           {"X-AI-Token": TOKEN})
        r = json.loads(body)
        # payload 摊在顶层：{"ok": true, "modules": [...]}
        n = len(r.get("modules") or (r.get("result") or {}).get("modules") or [])
        out["call_list"] = (st, r.get("ok"), n)
        if n != 1:
            bad.append("modules.list 返回模块数不对")
        out["call_unknown"] = json.loads(
            _req(base, "/ai/v1/call", {"tool": "nope.tool"}, {"X-AI-Token": TOKEN})[2]
        ).get("error")
        out["call_401"] = _req(base, "/ai/v1/call", {"tool": "status"}, {})[0]

        st, _, body = _req(base, "/api/module/get", {"key": "demo"})
        out["module_get"] = (st, json.loads(body).get("module", {}).get("title"))
        st, _, body = _req(base, "/api/module/export", {})
        out["module_export"] = (st, len(json.loads(body).get("export", {}).get("modules") or []))

        st, _, body = _req(base, "/")
        out["index"] = (st, "view-tools" in body, "modEdit" in body)
        out["ai_404"] = _req(base, "/ai/v1/nothing", headers={"X-AI-Token": TOKEN})[0]

        # /api/state 是 {"ok": true, "state": {...}} 信封；token 只能出现掩码
        st, _, body = _req(base, "/api/state")
        state = json.loads(body).get("state") or {}
        tok = (state.get("ai_api") or {}).get("token") or ""
        out["state_token"] = tok
        if tok and TOKEN in tok:
            bad.append("/api/state 明文回传 ai_api_token")
    finally:
        w.stop()

    for k, v in out.items():
        print(k, "=", v)
    if bad:
        print("FAIL", "；".join(bad))
        return 1
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
