"""配置持久化（settings.json）。"""
from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from typing import Any

from .paths import SETTINGS, ensure_dirs

log = logging.getLogger("settings")

DEFAULT_SUB = ""  # 首次运行留空，由用户在控制台里填写自己的订阅

DEFAULTS: dict[str, Any] = {
    # ——— 订阅 ———
    "sub_url": DEFAULT_SUB,
    "subs": [],                     # [{name,url,info,nodes,updated}]，sub_url 是当前订阅的镜像
    "sub_active": 0,                # 当前用的订阅下标
    "sub_update_hours": 6,          # 定时更新间隔（小时）；0=关闭
    "sub_ua": "clash.meta/v1.19.32",

    # ——— 端口 ———
    "mixed_port": 7891,             # 本地 HTTP/SOCKS 混合端口
    "controller_port": 9091,        # RESTful 控制端口
    "secret": "",                   # 首次启动自动生成

    # ——— 行为 ———
    "mode": "rule",                 # rule / global / direct
    "main_group": "节点选择",        # 主策略组名（订阅里的组名）
    "selected": {},                 # {组名: 节点名} 记住手动选择
    "system_proxy": True,           # 启动后接管 Windows 系统代理
    "autostart": False,             # 开机自启
    "start_minimized": True,
    "tun": False,                   # 预留：TUN 模式（当前关闭）

    # ——— 测速 ———
    "delay_url": "https://www.gstatic.com/generate_204",
    "delay_timeout_ms": 5000,
    "delay_interval_min": 20,       # 菜单节点延迟刷新间隔；0=关闭
    "auto_best": False,             # 定时把主组切到延迟最低节点
    "auto_best_interval_min": 30,
    "heal": True,                   # 节点失效时自动换到可用节点

    # ——— 移动热点特化（本版内置）———
    "mobile_aware": True,           # 认得出手机热点就切保守档 mihomo 参数
    "ai_block_regions": ["香港", "澳门", "中国大陆", "中国", "俄罗斯", "伊朗",
                         "朝鲜", "古巴", "叙利亚", "白俄罗斯"],   # OpenAI 拒绝的地区，不放进 AI 组
    "ai_prefer_regions": ["新加坡", "台湾", "日本", "美国", "韩国"],
    "net_change_restart": True,     # 热点重连 / 换网后自动重建内核
    "edition": "mobile",            # 发行版标记：mobile = 热点特化版

    # ——— AI 客户端专项（Claude Code / Codex）———
    "ai_enabled": True,             # AI 域名单独走「AI 稳定」组，不自动切换
    "ai_node": "",                  # 钉死的 AI 节点
    "ai_keep_alive": 30,            # 长连接保活（秒）

    # ——— 自动更新 ———
    "auto_update": True,            # 启动后静默查一次更新，有新版才提示
    "update_repo": "ayuanjc/VPNTray",  # GitHub 仓库 owner/name，留空则关闭更新检查
    "update_interval_hours": 12,    # 后台复查间隔（小时）；0=只在启动时查一次

    # ——— 其它 ———
    "log_level": "warning",
    "notify": True,
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif isinstance(v, list):
            out[k] = list(v)          # 复制，别把 DEFAULTS 里的 list 共享出去
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self) -> None:
        ensure_dirs()
        self._lock = threading.RLock()
        self._dirty = False           # 加载时补出来的默认值，下次真正 save 再落盘
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        raw: dict[str, Any] = {}
        if SETTINGS.exists():
            try:
                raw = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
                if not isinstance(raw, dict):
                    raise ValueError("settings.json 顶层不是对象")
            except Exception as e:  # noqa: BLE001
                # 千万别静默覆盖：先把坏文件留档，用户才有可能救回订阅列表
                bad = SETTINGS.with_name(f"settings.bad-{int(time.time())}.json")
                try:
                    SETTINGS.replace(bad)
                    log.error("settings.json 解析失败（%s），已备份到 %s", e, bad.name)
                except OSError as e2:
                    log.error("settings.json 解析失败（%s），备份也失败：%s", e, e2)
                raw = {}
        with self._lock:
            self.data = _deep_merge(copy.deepcopy(DEFAULTS), raw)
            if not self.data.get("secret"):
                self.data["secret"] = secrets.token_hex(16)
                self._dirty = True
        if self._dirty:
            try:
                self.save()
                self._dirty = False
            except OSError as e:
                log.error("首次写入 settings.json 失败：%s", e)

    def save(self) -> None:
        ensure_dirs()
        with self._lock:
            # 先序列化成快照，避免别的线程在 json.dump 迭代期间改 dict
            payload = json.dumps(self.data, ensure_ascii=False, indent=2, allow_nan=False)
            tmp_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=str(SETTINGS.parent),
                    delete=False, suffix=".tmp",
                ) as tmp:
                    tmp_name = tmp.name
                    tmp.write(payload)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                os.replace(tmp_name, SETTINGS)
            except BaseException:
                if tmp_name:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                raise

    # 便捷访问
    def __getitem__(self, k: str) -> Any:
        with self._lock:
            return self.data[k]

    def __setitem__(self, k: str, v: Any) -> None:
        with self._lock:
            self.data[k] = v

    def get(self, k: str, default: Any = None) -> Any:
        with self._lock:
            return self.data.get(k, default)

    def update(self, **kw: Any) -> None:
        with self._lock:
            self.data.update(kw)
        self.save()
