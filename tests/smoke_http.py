# -*- coding: utf-8 -*-
import json, sys, urllib.request, urllib.error
sys.path.insert(0, ".")
from vpntray import webui, modules

TOKEN = "smoke-token-0123456789abcdef0123456789"

class S(dict):
    def save(self): pass

class Stub:
    def __init__(self):
        self.settings = S(ai_api_token=TOKEN, ai_api_enabled=True, ai_api_level="full",
                          ai_enabled=False, ai_node="", scene_auto=True,
                          mixed_port=7891, mode="rule")
        self.modules = {}
    # 模块层
    def modules_status(self): return list(self.modules.values())
    def module_get(self, key): return self.modules.get(key)
    def module_save(self, payload):
        key = modules.normalize(payload).get("key") if isinstance(payload, dict) else None
        return (False, "stub")
    def module_delete(self, key): return (True, "")
    def module_reset(self): return (True, "")
    def module_export(self): return {"schema": 1, "modules": list(self.modules.values())}
    def module_import(self, payload): return (True, "")
    def modules_status_dict(self): return {}

stub = Stub()
stub.modules["demo"] = {"key": "demo", "title": "Demo", "kind": "scene"}

w = webui.WebUI(stub, on_quit=lambda: None, port=17892)
assert w.start(), "start failed"
port = w.port
base = "http://127.0.0.1:%d" % port

def get(path, headers=None):
    req = urllib.request.Request(base + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

def post(path, obj, headers=None):
    data = json.dumps(obj).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

out = {}
out["no_token"] = get("/ai/v1/manifest")[0]
out["bad_token"] = get("/ai/v1/manifest", {"X-AI-Token": "nope"})[0]
st, body = get("/ai/v1/manifest", {"X-AI-Token": TOKEN})
out["manifest"] = st
m = json.loads(body)
out["manifest_keys"] = sorted(m.keys())
out["tools"] = len(m.get("tools") or [])
out["bearer"] = get("/ai/v1/manifest", {"Authorization": "Bearer " + TOKEN})[0]
out["query_token"] = get("/ai/v1/manifest?token=" + TOKEN)[0]
st, body = post("/ai/v1/call", {"tool": "modules.list", "args": {}}, {"X-AI-Token": TOKEN})
r = json.loads(body)
out["call_list"] = (st, r.get("ok"), len((r.get("result") or {}).get("modules") or []))
st, body = post("/ai/v1/call", {"tool": "nope.tool"}, {"X-AI-Token": TOKEN})
out["call_unknown"] = json.loads(body).get("error")
out["call_401"] = post("/ai/v1/call", {"tool": "status"}, {})[0]
st, body = post("/api/module/get", {"key": "demo"})
out["module_get"] = (st, json.loads(body).get("module", {}).get("title"))
st, body = post("/api/module/export", {})
out["module_export"] = (st, len(json.loads(body).get("export", {}).get("modules") or []))
# 页面本身还能吐
st, body = get("/")
out["index"] = (st, "view-tools" in body, "apiLevel" in body, "modEdit" in body)
st, body = get("/ai/v1/nothing", {"X-AI-Token": TOKEN})
out["ai_404"] = (st, body[:40])
w.stop()
for k, v in out.items():
    print(k, "=", v)
