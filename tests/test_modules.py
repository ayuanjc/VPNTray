"""模块注册表 / AI 接口的离线测试：不联网、不起内核、不碰真实 data 目录。

跑法（仓库根目录）：
    python tests/test_modules.py
    python -m pytest tests -q

这些断言针对的是「改坏了会静默失效」的地方：模块落盘的原子性、策略组重名
拦截、纯规则模块的目标解析、AI 接口的 token 与权限级别。全部用临时目录，
跑完就删。
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import shutil
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vpntray import aiops, configgen, modules, paths  # noqa: E402


@contextlib.contextmanager
def sandbox():
    """把 modules 的落盘目录临时挪走，退出时恢复（含签名缓存）。"""
    old = paths.MODULES
    d = pathlib.Path(tempfile.mkdtemp(prefix="vpnmod-"))
    paths.MODULES = d
    modules._cache["sig"] = None
    try:
        yield d
    finally:
        paths.MODULES = old
        modules._cache["sig"] = None
        shutil.rmtree(d, ignore_errors=True)


def _mod(**kw):
    base = {
        "key": "epic",
        "title": "Epic 加速",
        "desc": "商店 / 启动器",
        "kind": "scene",
        "match": {"domains": ["epicgames.com", "unrealengine.com"]},
        "prefer_regions": ["香港", "日本"],
        "group": {"type": "select", "auto": True, "auto_title": "Epic 自动",
                  "probe": "https://www.epicgames.com/robots.txt"},
    }
    base.update(kw)
    return base


# ───────── 落盘 ─────────
def test_save_load_roundtrip():
    with sandbox() as d:
        ok, err = modules.save(_mod())
        assert ok, err
        assert (d / "epic.json").exists()          # 文件名就是 key
        got = modules.load("epic")
        assert got and got["title"] == "Epic 加速"
        assert got["match"]["domains"] == ["epicgames.com", "unrealengine.com"]
        assert got["schema"] == modules.SCHEMA
        # 没有 .tmp 残留（原子写）
        assert not list(d.glob("*.tmp"))


def test_key_from_filename():
    with sandbox() as d:
        (d / "mine.json").write_text(json.dumps({"title": "我的场景",
                                                 "match": {"domains": ["a.com"]}}),
                                     encoding="utf-8")
        modules._cache["sig"] = None
        got = modules.load("mine")
        assert got and got["key"] == "mine" and got["kind"] == "scene"


def test_broken_file_does_not_kill_all():
    with sandbox() as d:
        modules.save(_mod())
        (d / "bad.json").write_text("{ not json", encoding="utf-8")
        items = modules.load_all(force=True)
        assert [m["key"] for m in items] == ["epic"]


def test_update_keeps_other_fields():
    with sandbox():
        modules.save(_mod())
        ok, err = modules.update("epic", pinned="香港-IEPL 01")
        assert ok, err
        got = modules.load("epic")
        assert got["pinned"] == "香港-IEPL 01"
        assert got["match"]["domains"][0] == "epicgames.com"   # 没被冲掉


def test_remove():
    with sandbox():
        modules.save(_mod())
        assert modules.remove("epic")[0]
        assert modules.load("epic") is None
        assert modules.remove("epic")[0] is False


# ───────── 校验：能让内核起不来的输入必须在落盘前挡住 ─────────
def test_validate_errors():
    cases = [
        (_mod(key="Bad Key"), "key"),
        (_mod(key="ai"), "保留名"),
        (_mod(title=""), "组名"),
        (_mod(title="DIRECT"), "保留名"),
        (_mod(group={"type": "fallback", "auto": True}), "自动"),
        (_mod(group={"type": "weird"}), "组类型"),
        (_mod(match={"domains": ["*.bad domain"]}), "域名"),
        (_mod(match={"cidrs": ["10.0.0.0/33"]}), "网段"),
        (_mod(match={"rules": ["EXTERNAL,x"]}), "不支持"),
        (_mod(match={"rules": ["DOMAIN-SUFFIX,x.com,直连"]}), "目标组"),
        (_mod(match={}), "至少填一个"),
    ]
    for raw, hint in cases:
        errs = modules.validate(modules.normalize(raw))
        assert errs, f"没拦住：{raw}"
        assert any(hint in e for e in errs), f"{raw} → {errs}"


def test_duplicate_title_rejected():
    with sandbox():
        assert modules.save(_mod())[0]
        ok, err = modules.save(_mod(key="epic2"))     # 同名组会让 mihomo 起不来
        assert not ok and "组名" in err


def test_domain_normalized():
    with sandbox():
        modules.save(_mod(match={"domains": ["*.Foo.COM.", "+.bar.com", "baz.com"]}))
        got = modules.load("epic")
        assert got["match"]["domains"] == ["foo.com", "bar.com", "baz.com"]


def test_caps():
    raw = _mod(match={"domains": [f"d{i}.com" for i in range(modules.MAX_DOMAINS + 20)]})
    got = modules.normalize(raw)
    assert len(got["match"]["domains"]) == modules.MAX_DOMAINS


# ───────── 导入 / 导出 ─────────
def test_import_partial():
    with sandbox():
        # key 会被 normalize 成小写，所以「大写 key」不算坏：这里用非法组类型
        payload = {"modules": [_mod(), _mod(key="ok2", title="第二个场景"),
                              _mod(key="bad2", title="坏的", group={"type": "weird"})]}
        ok, errs = modules.import_all(payload)
        assert ok == 2 and len(errs) == 1
        assert "bad2" in errs[0]
        assert modules.load("ok2") is not None


def test_export_shape():
    with sandbox():
        modules.save(_mod())
        ex = modules.export_all()
        assert ex["schema"] == modules.SCHEMA and len(ex["modules"]) == 1
        assert "exported" in ex


# ───────── 内置模块：首次物化，删了不复活 ─────────
def test_ensure_builtins_with_real_settings():
    """回归：真 Settings 没有 __contains__ 时，ensure_builtins 里
    「x in settings」会退化成 __getitem__(0) 直接 KeyError(0)，程序启动即崩。
    这里用真的 Settings 类（只把落盘路径挪到临时目录）跑一遍物化。"""
    from vpntray import settings as settings_mod

    with sandbox() as d:
        old_path = settings_mod.SETTINGS
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="vpnsets-"))
        settings_mod.SETTINGS = tmp / "settings.json"
        try:
            s = settings_mod.Settings()
            s["steam_enabled"] = False
            s["steam_prefer_regions"] = ["日本"]
            assert "steam_enabled" in s          # __contains__ 现在真的在
            assert 0 not in s                    # 别再把整数键当迭代入口
            made = modules.ensure_builtins(s)
            assert made == 2
            steam = json.loads((d / "steam.json").read_text(encoding="utf-8"))
            assert steam["enabled"] is False
            assert steam["prefer_regions"][0] == "日本"
        finally:
            settings_mod.SETTINGS = old_path
            shutil.rmtree(tmp, ignore_errors=True)


def test_builtins_seed_once():
    with sandbox() as d:
        assert modules.ensure_builtins() == 2
        assert {m["key"] for m in modules.load_all()} == {"steam", "github"}
        assert modules.ensure_builtins() == 0            # 第二次不做事
        modules.remove("steam")
        assert modules.ensure_builtins() == 0            # 删掉的不复活
        assert modules.load("steam") is None
        assert modules.reset_builtins() >= 1             # 手动恢复才有
        assert modules.load("steam") is not None


# ───────── 翻译成 mihomo 配置 ─────────
def _sub():
    # 两个节点：自动子组（fallback）只在成员 >=2 时才会建
    return {"proxies": [{"name": "HK-1", "type": "ss", "server": "1.2.3.4", "port": 443,
                         "cipher": "aes-128-gcm", "password": "x"},
                        {"name": "JP-1", "type": "ss", "server": "5.6.7.8", "port": 443,
                         "cipher": "aes-128-gcm", "password": "y"}],
            "proxy-groups": [{"name": "节点选择", "type": "select",
                              "proxies": ["HK-1", "JP-1"]}],
            "rules": ["MATCH,DIRECT"], "info": [], "providers": {}}


def _settings(**kw):
    s = {"mixed_port": 7891, "controller_port": 9091, "ai_enabled": False,
         "scene_auto": True}
    s.update(kw)
    return s


def test_build_scene_module():
    with sandbox():
        modules.save(_mod())
        cfg = configgen.build(_sub(), _settings(), {"HK-1": 30, "JP-1": 80})
        groups = [g["name"] for g in cfg["proxy-groups"]]
        assert "Epic 加速" in groups and "Epic 自动" in groups
        assert "DOMAIN-SUFFIX,epicgames.com,Epic 加速" in cfg["rules"]
        assert cfg["rules"][-1].startswith("MATCH,")


def test_build_rule_module_targets_main():
    with sandbox():
        modules.save({"key": "lan-direct", "title": "局域网直连", "kind": "rule",
                      "group": {"target": ""},
                      "match": {"domains": ["intranet.example.com"],
                                "cidrs": ["10.10.0.0/16"]}})
        cfg = configgen.build(_sub(), _settings(), {"HK-1": 30, "JP-1": 80})
        rules = cfg["rules"]
        assert any(r.startswith("DOMAIN-SUFFIX,intranet.example.com,") for r in rules)
        assert any(r.startswith("IP-CIDR,10.10.0.0/16,") for r in rules)
        # 纯规则模块不建组
        assert "局域网直连" not in [g["name"] for g in cfg["proxy-groups"]]


def test_build_skips_duplicate_group():
    with sandbox():
        modules.save(_mod())
        sub = _sub()
        sub["proxy-groups"].append({"name": "Epic 加速", "type": "select",
                                    "proxies": ["HK-1"]})
        cfg = configgen.build(sub, _settings(), {"HK-1": 30})
        names = [g["name"] for g in cfg["proxy-groups"]]
        assert names.count("Epic 加速") == 1          # 订阅里的那个赢，模块跳过


def test_build_disabled_module_ignored():
    with sandbox():
        modules.save(_mod(enabled=False))
        cfg = configgen.build(_sub(), _settings(), {"HK-1": 30, "JP-1": 80})
        assert "Epic 加速" not in [g["name"] for g in cfg["proxy-groups"]]
        assert not any("epicgames.com" in r for r in cfg["rules"])


# ───────── AI 接口 ─────────
class _Settings(dict):
    def save(self):
        self["_saved"] = self.get("_saved", 0) + 1


class _App:
    def __init__(self, level="full", enabled=True):
        self.settings = _Settings({"ai_api_level": level, "ai_api_enabled": enabled})
        self.state = {}
        self.saved_modules = []

    # 工具处理器用到的最小面
    def modules_status(self):
        return [{"key": "epic", "title": "Epic 加速", "kind": "scene", "enabled": True}]

    def module_get(self, key):
        return {"key": key, "title": "Epic 加速"} if key == "epic" else None

    def module_save(self, payload):
        self.saved_modules.append(payload)
        return True, ""

    def ai_status(self):
        return {"on": True, "node": "HK-1"}


def test_token_autogenerated_and_stable():
    app = _App()
    t1 = aiops.ensure_token(app.settings)
    t2 = aiops.ensure_token(app.settings)
    assert t1 == t2 and len(t1) > 20
    assert app.settings[aiops.TOKEN_KEY] == t1
    assert app.settings["_saved"] == 1          # 只在首次生成时落盘


def test_auth():
    app = _App()
    tok = aiops.ensure_token(app.settings)
    assert aiops.auth(app, {"X-AI-Token": tok})[0]
    assert aiops.auth(app, {"Authorization": "Bearer " + tok})[0]
    assert aiops.auth(app, {}, tok)[0]
    ok, err = aiops.auth(app, {"X-AI-Token": "wrong"})
    assert not ok and "token" in err
    off = _App(enabled=False)
    aiops.ensure_token(off.settings)
    assert not aiops.auth(off, {"X-AI-Token": off.settings[aiops.TOKEN_KEY]})[0]


def test_manifest_shape():
    app = _App(level="read")
    mf = aiops.manifest(app)
    assert mf["ok"] and mf["api"] == "vpn-tray-ai"
    names = {t["name"] for t in mf["tools"]}
    assert "modules.list" in names and "app.quit" in names
    read_only = [t for t in mf["tools"] if t["allowed"]]
    assert read_only and all(t["level"] == "read" for t in read_only)


def test_call_level_gate():
    app = _App(level="read")
    res = aiops.call(app, "modules.save", {"module": _mod()})
    assert res["ok"] is False and res["need"] == "write"
    assert not app.saved_modules
    # 只读工具照样能用
    res = aiops.call(app, "modules.list", {})
    assert res["ok"] and res["modules"][0]["key"] == "epic"


def test_call_unknown_tool():
    app = _App()
    res = aiops.call(app, "module.sav", {})
    assert res["ok"] is False and "没有这个工具" in res["error"]
    assert res["hint"]


def test_call_write_success():
    app = _App(level="write")
    res = aiops.call(app, "modules.save", {"module": _mod()})
    assert res["ok"] and app.saved_modules
    assert res["modules"][0]["key"] == "epic"


def test_state_shape():
    app = _App()
    st = aiops.state(app)
    assert st["enabled"] and st["level"] == "full" and st["tools"] > 10
    assert st["token"] and st["path"] == "/ai/v1/call"


def test_tool_table_consistency():
    for name, t in aiops.TOOLS.items():
        assert name == t["name"]
        assert t["level"] in aiops.LEVELS
        assert name in aiops._HANDLERS
        assert t["title"] and t["desc"]
        assert isinstance(t["params"], dict)


if __name__ == "__main__":
    bad = 0
    for k, fn in list(globals().items()):
        if not k.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {k}")
        except Exception:  # noqa: BLE001
            bad += 1
            print(f"  FAIL  {k}")
            traceback.print_exc()
    print("失败 %d 个" % bad if bad else "全部通过")
    sys.exit(1 if bad else 0)
