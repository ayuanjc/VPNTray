"""配置持久化（settings.json）。"""
from __future__ import annotations

import base64
import copy
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from typing import Any

from .paths import MODULES, SETTINGS, ensure_dirs, fallback_state

log = logging.getLogger("settings")

# 配置结构版本：2 = 场景键搬进 data/modules/*.json，settings 不再保存 steam_*/github_*
SCHEMA_VERSION = 2

# 这几个字段是凭据：写盘前用 DPAPI 封装，只有当前 Windows 账户能解开
SECRET_KEYS = ("secret", "ai_api_token")
_DPAPI_PREFIX = "dpapi:v1:"

# 2.x 遗留键：模块首次物化时读它们，物化完就没用了
LEGACY_KEYS = (
    "steam_enabled", "steam_node", "steam_prefer_regions",
    "github_enabled", "github_node", "github_prefer_regions",
)

DEFAULT_SUB = ""  # 首次运行留空，由用户在控制台里填写自己的订阅

DEFAULTS: dict[str, Any] = {
    # ——— 订阅 ———
    "sub_url": DEFAULT_SUB,
    "subs": [],                     # [{name,url,info,nodes,updated}]，sub_url 是当前订阅的镜像
    "sub_active": 0,                # 当前用的订阅下标
    "sub_update_hours": 6,          # 定时更新间隔（小时）；0=关闭
    "sub_ua": "clash.meta/v1.19.32",
    "sub_mode": "single",           # single = 只用当前订阅；merge = 多条订阅节点合并
    "sub_hash": "",                 # 上次成功下载的订阅指纹，用来判断要不要重建配置

    # ——— 端口 ———
    "mixed_port": 7891,             # 本地 HTTP/SOCKS 混合端口
    "controller_port": 9091,        # RESTful 控制端口
    "secret": "",                   # 首次启动自动生成（写盘时 DPAPI 封装）
    # 配置结构版本。默认 1 表示「老结构，可能还带 steam_* 之类的遗留键」；
    # 加载时发现根本没有遗留键才是 2 —— 直接默认成 2 会让 migrate() 永远早退。
    "schema_version": 1,

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
    "dns_guard": True,              # 本地 DNS 兜底：127.0.0.1:53，给 Node/c-ares 类程序用
    "probe_on_select": True,        # 手动切节点后自动查一次出口 IP/纯净度

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

    # ——— 场景加速（Steam / GitHub）———
    # 各自一个 select 组，默认挑偏好地区里排最前的节点；钉住后不会被自动
    # 优选/自愈改掉（这两个场景换 IP 不触发风控，但要保证出口稳定不绕路）。
    "steam_enabled": True,
    "steam_node": "",
    "steam_prefer_regions": ["香港", "台湾", "日本", "韩国", "新加坡"],
    "github_enabled": True,
    "github_node": "",
    "github_prefer_regions": ["香港", "日本", "新加坡", "台湾", "美国"],
    # GitHub 组里带一个 fallback 自动子组（只有 GitHub 场景有）：机场节点
    # 大面积抖动时靠它自动跳过死节点，不用手工一个个试。
    "scene_auto": True,
    "github_heal": True,            # 自检失败时自动把出口切到能连上的节点
    "git_proxy_sync": True,         # 启动时把 git 全局 http(s).proxy 对齐到混合端口

    # ——— 自动更新 ———
    "auto_update": True,            # 启动后静默查一次更新，有新版才提示
    "update_repo": "ayuanjc/VPNTray",  # GitHub 仓库 owner/name，留空则关闭更新检查
    "update_interval_hours": 12,    # 后台复查间隔（小时）；0=只在启动时查一次

    # ——— 其它 ———
    "log_level": "warning",
    "notify": True,
    "agreed_version": "",           # 用户点过「知道了」的版本号，只用于首次提示

    # ——— 运行时环境（由 paths 的启动探测写入，界面/诊断页只读展示）———
    "data_dir": "",                 # 实际生效的数据目录，可能与 exe 所在目录不同
    "data_dir_fallback": False,     # True = 原数据目录不可写，已回退到 %LOCALAPPDATA%
}


def port_setting(settings, key: str, default: int) -> int:
    """端口必须是 1024..65535 的整数，脏值回落默认值。

    kernel / configgen 以前各抄了一份一模一样的实现，改端口规则要改两处。
    """
    try:
        v = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return v if 1024 <= v <= 65535 else default


def _coerce(default: Any, value: Any) -> tuple[Any, bool]:
    """按 DEFAULTS 里的类型收敛一个值，返回 (收敛后的值, 是否可接受)。

    手改过的 settings.json 会把 "subs" 写成字符串、把 "mixed_port" 写成 null，
    原样落盘之后 kernel / configgen / subscription 每处调用都得自己防一遍，
    漏一处就是「点开始没反应」这种从界面上查不出来的故障。默认值的类型就是
    schema：类型不符一律回落默认值，并让调用方记下被修正的键。
    """
    if isinstance(default, bool):
        # bool 是 int 的子类，判断顺序必须先 bool，否则 "tun": 1 会被当成整数
        if isinstance(value, bool):
            return value, True
        if isinstance(value, int) and value in (0, 1):
            return bool(value), True
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
            return value.strip().lower() in ("true", "1"), True
        return default, False
    if isinstance(default, int):
        if isinstance(value, bool):
            return default, False
        if isinstance(value, int):
            return value, True
        if isinstance(value, float) and value.is_integer():
            return int(value), True
        if isinstance(value, str):
            try:
                return int(value.strip()), True
            except ValueError:
                return default, False
        return default, False
    if isinstance(default, float):
        if isinstance(value, bool):
            return default, False
        if isinstance(value, (int, float)):
            return float(value), True
        if isinstance(value, str):
            try:
                return float(value.strip()), True
            except ValueError:
                return default, False
        return default, False
    if isinstance(default, str):
        # 数字写进字符串字段多半是手工编辑笔误，不猜，直接回落
        return (value, True) if isinstance(value, str) else (default, False)
    if isinstance(default, list):
        return (list(value), True) if isinstance(value, list) else (default, False)
    if isinstance(default, dict):
        return (dict(value), True) if isinstance(value, dict) else (default, False)
    return value, True


def _deep_merge(base: dict, over: dict, fixed: list[str] | None = None,
                _prefix: str = "") -> dict:
    """把磁盘上的 over 合并进 base，顺手做类型校验。

    fixed 非 None 时，被修正的键名（带层级前缀）会追加进去 —— 上层据此备份
    用户原文件并打一条 warning，否则用户只会发现「我的设置怎么自己变回去了」。
    """
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            # 只在「默认值也是 dict」时递归（selected 这种键名是动态的），
            # 里面的值不做类型校验，否则会把用户自己加的键当成脏值改掉
            out[k] = _deep_merge(out[k], v, fixed, f"{_prefix}{k}.")
        elif k in base:
            val, ok = _coerce(base[k], v)
            if not ok and fixed is not None:
                fixed.append(f"{_prefix}{k}")
            out[k] = list(val) if isinstance(val, list) else val
        else:
            # DEFAULTS 里没有的键（旧版本残留、别人加的扩展键）原样保留，
            # 不在这里删：删了会破坏别人正在用的配置
            out[k] = v
    return out


def _dpapi(data: bytes, protect: bool) -> bytes | None:
    """用当前 Windows 账户的 DPAPI 加/解密一段字节；非 Windows 返回 None。

    为什么不用加密库：settings.json 里躺着 controller secret 和 AI 接口 token，
    任何一个能读到文件的同机账户（备份程序、另一个用户）都能直接接管内核。
    DPAPI 不需要密码、不需要新依赖，密钥由系统按账户派生 —— 换机器/换账户解
    不开，这正是我们要的语义（换机重新生成一个 secret 即可）。
    """
    if os.name != "nt" or not data:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(bytes(data), len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        fn.restype = wintypes.BOOL
        # CRYPTPROTECT_UI_FORBIDDEN：后台进程绝不能因为弹窗卡住
        ok = fn(ctypes.byref(blob_in), None, None, None, None, 0x1,
                ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.WinDLL("kernel32").LocalFree(blob_out.pbData)
    except Exception as e:  # noqa: BLE001
        log.warning("DPAPI %s失败：%s", "封装" if protect else "解封", e)
        return None


def _seal(value: str) -> str:
    """明文 → "dpapi:v1:<b64>"；封装失败就原样返回明文（不能因此存不上配置）。"""
    if not value or value.startswith(_DPAPI_PREFIX):
        return value
    raw = _dpapi(value.encode("utf-8"), True)
    if not raw:
        return value
    return _DPAPI_PREFIX + base64.b64encode(raw).decode("ascii")


def _unseal(value: str) -> str | None:
    """解封；不是封装串就原样返回。解不开返回 None（调用方按空值处理）。"""
    if not isinstance(value, str) or not value.startswith(_DPAPI_PREFIX):
        return value
    try:
        raw = base64.b64decode(value[len(_DPAPI_PREFIX):])
    except Exception:  # noqa: BLE001
        return None
    plain = _dpapi(raw, False)
    if plain is None:
        return None
    return plain.decode("utf-8", "replace")


def _salvage(text: str) -> dict[str, Any]:
    """settings.json 整体解析失败时，逐键把还能认的值捞回来。

    为什么值得这么做：一个尾逗号或一处手改错误会让整个文件进备份，订阅地址、
    节点选择、AI token 全部复位 —— 用户看到的只是「我的配置没了」。这里按已知
    键名切片单独 json 解析，能救几个是几个。
    """
    out: dict[str, Any] = {}
    if not text.strip():
        return out
    try:
        obj = json.loads(re.sub(r",\s*([}\]])", r"\1", text))
        if isinstance(obj, dict):
            return obj
    except Exception:  # noqa: BLE001
        pass
    for k, default in DEFAULTS.items():
        if k in ("secret", "ai_api_token"):
            continue                # 凭据解封要整套流程，救不回来就重新生成
        m = re.search(r'"%s"\s*:\s*' % re.escape(k), text)
        if not m:
            continue
        rest = text[m.end():]
        nxt = re.search(r'\n\s{1,4}"[A-Za-z0-9_]+"\s*:', rest)
        chunk = (rest[:nxt.start()] if nxt else rest).strip().rstrip(",").strip()
        try:
            val = json.loads(chunk)
        except Exception:  # noqa: BLE001
            continue
        val, ok = _coerce(default, val)
        if ok:
            out[k] = val
    return out


# 收紧权限失败只记一次：save() 可能被连续调用，icacls 失败会刷满日志
_ACL_WARNED = False


def _current_user() -> str:
    """icacls 能认的当前账户名。"""
    name = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if not name:
        return ""
    # 域账户要带域前缀；微软账户登录时 USERDOMAIN 就是机器名，带上同样能解析
    return f"{domain}\\{name}" if domain else name


def _harden_acl(path) -> None:
    """把配置文件的权限收到「只有当前用户」。失败只记 warning，不阻断保存。

    settings.json 里躺着 mihomo 的 controller secret、AI 接口 token 和带机场
    token 的订阅地址；它默认继承用户目录的 ACL，同机其它账户、以及以别的身份
    跑的备份/同步程序都能读。收紧权限属于加固，读不到也不该让存配置失败。
    """
    global _ACL_WARNED
    if os.name != "nt":
        return                      # 其它平台靠家目录权限/umask，没有 icacls
    user = _current_user()
    if not user:
        return
    # /inheritance:r 去掉继承来的 Everyone/Users，/grant:r 只留当前用户；
    # 给全权而不是只读：下次保存仍要覆盖这个文件（os.replace 会用新文件替换它）
    try:
        p = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
            capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        if not _ACL_WARNED:
            _ACL_WARNED = True
            log.warning("收紧 %s 权限失败（%s），配置仍已保存", path.name, e)
        return
    if p.returncode != 0 and not _ACL_WARNED:
        _ACL_WARNED = True
        detail = (p.stderr or b"").decode("utf-8", "replace").strip()[:200]
        log.warning("收紧 %s 权限失败（icacls=%s）：%s", path.name, p.returncode, detail)


class Settings:
    def __init__(self) -> None:
        ensure_dirs()
        self._lock = threading.RLock()
        self._dirty = False           # 加载时补出来的默认值，下次真正 save 再落盘
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        raw: dict[str, Any] = {}
        raw_bytes: bytes | None = None
        if SETTINGS.exists():
            try:
                raw_bytes = SETTINGS.read_bytes()
                raw = json.loads(raw_bytes.decode("utf-8-sig"))
                if not isinstance(raw, dict):
                    raise ValueError("settings.json 顶层不是对象")
            except Exception as e:  # noqa: BLE001
                # 千万别静默覆盖：先把坏文件留档，用户才有可能救回订阅列表
                text = ""
                if raw_bytes is not None:
                    text = raw_bytes.decode("utf-8-sig", "replace")
                bad = SETTINGS.with_name(f"settings.bad-{int(time.time())}.json")
                try:
                    SETTINGS.replace(bad)
                    log.error("settings.json 解析失败（%s），已备份到 %s", e, bad.name)
                except OSError as e2:
                    log.error("settings.json 解析失败（%s），备份也失败：%s", e, e2)
                # 整份解析不了不等于每个键都坏了：逐键抢救，剩下的才回落默认值
                raw = _salvage(text)
                if raw:
                    log.warning("settings.json 已逐键抢救出 %d 个字段", len(raw))
                raw_bytes = text.encode("utf-8") if text else None
        fixed: list[str] = []
        merged = _deep_merge(copy.deepcopy(DEFAULTS), raw, fixed)
        if fixed:
            # 用户手写的值被回落之前先留一份原件，否则「我的订阅列表去哪了」
            # 连找回的凭据都没有
            self._backup_bad(raw_bytes, "字段类型不对")
            log.warning("settings.json 有 %d 个字段类型不对，已回落默认值：%s",
                        len(fixed), ", ".join(sorted(fixed)))
        # 凭据在磁盘上是 DPAPI 封装串，内存里必须是明文（内核、webui、aiops 都用明文）
        for k in SECRET_KEYS:
            v = merged.get(k)
            if isinstance(v, str) and v.startswith(_DPAPI_PREFIX):
                plain = _unseal(v)
                if plain is None:
                    # 换机器/换账户后解不开：当作空值，下面会重新生成 secret
                    log.warning("%s 无法解封（换账户或换机器？），按空值处理", k)
                    merged[k] = ""
                else:
                    merged[k] = plain
        with self._lock:
            self.data = merged
            env = fallback_state()
            self.data["data_dir"] = env["data_dir"]
            self.data["data_dir_fallback"] = env["data_dir_fallback"]
            if not any(k in merged for k in LEGACY_KEYS):
                # 文件里没有遗留场景键（新装或已迁移过）：直接标成当前版本，
                # 不必等 migrate() 走一遍
                if int(merged.get("schema_version") or 1) < SCHEMA_VERSION:
                    self.data["schema_version"] = SCHEMA_VERSION
            if not self.data.get("secret"):
                # 已有 secret 就绝不重建：正在跑的内核和 UI 都拿它做 controller
                # 校验，换掉等于当场 401
                self.data["secret"] = secrets.token_hex(16)
                self._dirty = True
            if fixed:
                self._dirty = True
        if self._dirty:
            try:
                self.save()
                self._dirty = False
            except OSError as e:
                log.error("首次写入 settings.json 失败：%s", e)

    def _backup_bad(self, raw: bytes, why: str) -> None:
        """把出问题的 settings.json 原样留一份，再按默认值继续跑。

        为什么必须留原文件：类型被修正之后，被改掉的原值就从内存里消失了——用户到底
        手改过什么、订阅地址和节点还在不在，只能靠这份档案回查。解析失败也走这里，
        所以本方法绝不抛：备份失败只留一条 warning，不能让启动流程断掉。
        """
        path = SETTINGS.with_name(f"settings.json.bad-{int(time.time())}")
        try:
            path.write_bytes(raw)
            log.warning("settings.json 已备份为 %s（%s）", path.name, why)
        except OSError as e:  # noqa: BLE001
            log.warning("备份 settings.json 失败（%s）：%s", why, e)


    def migrate(self) -> None:
        """按 schema_version 做一次性结构迁移。

        2.5.9 起场景/模块的唯一真相是 data/modules/*.json，settings 里的
        steam_*/github_* 只是首次物化的取值来源。留着它们的后果是「我改了设置
        怎么没生效」—— 改的正是没人再读的键。物化成功（模块目录非空）之后才删，
        否则用户自定的偏好会白丢。
        """
        with self._lock:
            if int(self.data.get("schema_version") or 1) >= SCHEMA_VERSION:
                return
            try:
                seeded = any(MODULES.glob("*.json"))
            except OSError:
                seeded = False
            if not seeded:
                log.info("模块尚未物化，跳过设置结构迁移（下次启动再试）")
                return
            dropped = [k for k in LEGACY_KEYS if k in self.data]
            for k in dropped:
                del self.data[k]
            self.data["schema_version"] = SCHEMA_VERSION
        if dropped:
            log.info("设置结构迁移到 v%d：清理 %d 个旧场景键（%s）",
                     SCHEMA_VERSION, len(dropped), ", ".join(dropped))
        try:
            self.save()
        except OSError as e:
            log.warning("迁移后保存设置失败：%s", e)

    def save(self) -> None:
        ensure_dirs()
        with self._lock:
            # 先序列化成快照，避免别的线程在 json.dump 迭代期间改 dict
            plain = dict(self.data)
            for k in SECRET_KEYS:
                v = plain.get(k)
                if isinstance(v, str) and v:
                    plain[k] = _seal(v)
            payload = json.dumps(plain, ensure_ascii=False, indent=2, allow_nan=False)
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

        # 权限收紧放在锁外：icacls 是外部进程，最坏要等 5 秒，不能顺手把设置锁
        # 一起占住（UI 线程取设置时会跟着卡）
        _harden_acl(SETTINGS)

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

    def __contains__(self, k: object) -> bool:
        # 没有它的时候，`x in settings` 会退化成 __getitem__(0) 的迭代协议，
        # 直接 KeyError(0)（2.5.9 启动即崩就是这么来的）。
        with self._lock:
            return k in self.data

    def update(self, **kw: Any) -> None:
        with self._lock:
            self.data.update(kw)
        self.save()
