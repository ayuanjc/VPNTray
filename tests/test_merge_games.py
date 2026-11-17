"""游戏模块合并（1.3.0 merge_games）的离线测试：不联网、不碰真实 data 目录。

为什么单独测：合并会把旧 json 改成 enabled=false + merged_into，是一次"不可见"的
破坏性写盘。如果幂等性写错，用户每次启动都重写一遍自己的模块文件；如果 order 算错，
合并后的组抢不过旧组，界面切了没反应 —— 两种都不会报错，只会让人怀疑软件坏了。
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vpntray import modules, paths  # noqa: E402


@contextlib.contextmanager
def sandbox():
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


def _game(key, title, domains, order=60, probe="https://example.com/robots.txt",
          regions=None, exclude=None):
    match = {"domains": list(domains)}
    if exclude:
        match["exclude"] = list(exclude)
    mod = {
        "key": key,
        "title": title,
        "desc": title + " 商店 / 启动器",
        "kind": "scene",
        "order": order,
        "match": match,
        "prefer_regions": list(regions or ["香港", "日本"]),
        "group": {"type": "select", "auto": True, "auto_title": title + " 自动",
                  "probe": probe},
    }
    ok, msg = modules.save(mod)
    assert ok, msg
    return mod


def _by_key():
    return {m["key"]: m for m in modules.load_all(force=True)}


def test_merge_creates_games_module():
    with sandbox():
        _game("xbox", "Xbox 加速", ["xboxlive.com", "xbox.com"], order=61)
        _game("epic", "Epic 加速", ["epicgames.com", "unrealengine.com"], order=62)
        _game("riot", "Riot 加速", ["leagueoflegends.com", "riotgames.com"], order=63)
        assert modules.merge_games() == 3
        m = _by_key()
        games = m["games"]
        assert games["enabled"] is True
        assert games["title"] == "游戏加速"
        assert games["group"]["type"] == "select"
        assert games["group"]["auto"] is True
        dom = games["match"]["domains"]
        for d in ("xboxlive.com", "epicgames.com", "leagueoflegends.com"):
            assert d in dom
        assert len(dom) == len(set(dom))              # 合并后不许有重复域
        assert games["order"] < 61                    # 排在所有旧模块之前


def test_old_modules_marked_and_disabled():
    with sandbox():
        _game("xbox", "Xbox 加速", ["xboxlive.com"])
        _game("epic", "Epic 加速", ["epicgames.com"])
        modules.merge_games()
        m = _by_key()
        for key in ("xbox", "epic"):
            assert m[key]["enabled"] is False
            assert m[key]["merged_into"] == "games"
        assert modules.summary() and any(
            x["merged_into"] == "games" for x in modules.summary())


def test_merge_is_idempotent():
    with sandbox():
        _game("xbox", "Xbox 加速", ["xboxlive.com"])
        _game("epic", "Epic 加速", ["epicgames.com"])
        modules.merge_games()
        # 用户改名之后再次启动：不许覆盖用户的选择，也不许重复标记
        games = _by_key()["games"]
        games["title"] = "我的游戏"
        ok, msg = modules.save(games)
        assert ok, msg
        assert modules.merge_games() == 0
        assert _by_key()["games"]["title"] == "我的游戏"


def test_single_game_module_is_left_alone():
    with sandbox():
        _game("xbox", "Xbox 加速", ["xboxlive.com"])
        assert modules.merge_games() == 0
        m = _by_key()
        assert "games" not in m
        assert m["xbox"]["enabled"] is True


def test_merge_carries_exclude_and_probe():
    with sandbox():
        _game("xbox", "Xbox 加速", ["xboxlive.com"], order=61,
              probe="https://www.xbox.com/robots.txt", exclude=["dl.xbox.com"])
        _game("epic", "Epic 加速", ["epicgames.com"], order=62)
        modules.merge_games()
        games = _by_key()["games"]
        assert "dl.xbox.com" in games["match"]["exclude"]
        assert games["group"]["probe"] == "https://www.xbox.com/robots.txt"


def test_merged_modules_do_not_rebuild_groups():
    with sandbox():
        _game("xbox", "Xbox 加速", ["xboxlive.com"])
        _game("epic", "Epic 加速", ["epicgames.com"])
        modules.merge_games()
        keys = [m["key"] for m in modules.load_all(force=True)
                if m.get("enabled", True) and not m.get("merged_into")]
        assert "games" in keys
        assert "xbox" not in keys and "epic" not in keys


# ── 1.3.0：老机器上的内置模块 json 要按新定义刷新 ──────────────────────
def test_refresh_updates_old_google_match():
    """旧 google.json 里的宽域与 keyword 必须被换掉，用户的开关与钉住保留。

    这是真机撞出来的：内置模块是首次运行物化的 json，改 _LAUNCHERS 只影响新装的
    机器；老机器上 DOMAIN-KEYWORD,googleapis 一直在，把 services.googleapis.cn
    这种国内服务一起抢到日本，而界面上看不出任何异常。
    """
    with sandbox() as d:
        old = {
            "key": "google", "title": "Google 反重力", "kind": "scene",
            "enabled": False, "order": 70, "builtin": True,
            "pinned": "JP-01",
            "group": {"type": "select", "auto": True, "auto_title": "Google 自动",
                      "probe": "https://old.example/robots.txt",
                      "interval": 180, "timeout": 3000},
            "match": {"domains": ["googleapis.com", "gemini.google.com"],
                      "rules": ["DOMAIN-KEYWORD,googleapis"]},
        }
        (d / "google.json").write_text(json.dumps(old, ensure_ascii=False),
                                      encoding="utf-8")
        modules._cache["sig"] = None
        assert modules.refresh_builtins() == 1
        got = json.loads((d / "google.json").read_text(encoding="utf-8"))
        assert "googleapis.com" not in got["match"]["domains"]
        assert got["match"]["rules"] == []
        assert "dl.google.com" in got["match"]["exclude"]
        assert got["builtin_rev"] == modules.BUILTIN_REV
        # 用户的选择不动：停用仍是停用，钉住的节点还在，序号不改
        assert got["enabled"] is False
        assert got["pinned"] == "JP-01"
        assert got["order"] == 70
        assert got["group"]["type"] == "select"
        # 探测地址属于定义：换了域名就得换探测
        assert got["group"]["probe"].startswith("https://gemini.google.com")
        # 幂等：第二次没有可刷的
        assert modules.refresh_builtins() == 0


def test_refresh_skips_merged_module():
    """已经并进别处的模块不许被刷活 —— 否则配置里立刻多出一批重复组。"""
    with sandbox() as d:
        old = {"key": "google", "title": "Google", "kind": "scene",
               "enabled": False, "merged_into": "games", "builtin_rev": 1,
               "match": {"domains": ["googleapis.com"]}}
        p = d / "google.json"
        p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        modules._cache["sig"] = None
        assert modules.refresh_builtins() == 0
        got = json.loads(p.read_text(encoding="utf-8"))
        assert got["match"]["domains"] == ["googleapis.com"]
        assert got.get("merged_into") == "games"


def test_refresh_does_not_create_missing_module():
    """用户删掉的模块不复活：refresh 只改已有文件。"""
    with sandbox():
        assert modules.refresh_builtins() == 0

