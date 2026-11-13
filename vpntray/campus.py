"""校园网自动登录与适配。

为什么单独一个模块：校园网（Dr.COM / 深澜 / 华为 ePortal / 各家自建门户）
在认证前会把所有 HTTP 劫持到认证页，认证后限速、按会话掉线、切网关。
它跟「代理内核」是两层网络，混在一起会互相掩盖故障（断网急救那 13 项体检
默认认为链路坏，其实只是没过认证）。

对外三件事：
  discover()  —— 现在通不通、认证页在哪、像哪种门户
  login()     —— 按 provider 适配器登一次，登完再验一次
  tick()/Guard—— 守护线程：掉线就自动登，登不上就退避 + 提示

provider 适配器（都由「门户实际长什么样」决定，而不是靠猜域名）：
  drcom   Dr.COM 5/6 参数式接口 /drcom/login?...
  srun    深澜 /cgi-bin/srun_portal（部分版本要 MD5/自定义模板，见 README）
  eportal 华为/H3C ePortal 表单
  portal  通用表单：抓门户页第一个带 password 的 form，按名称启发式填
  custom  用户自己粘贴的登录 URL 模板（{user} {pass} {ip} {mac} {portal}）

设计取舍：
- 一律 trust_env=False 且不走系统代理。认证页是内网地址，走代理必然失败，
  而且代理内核这时候往往还没起来。
- 密码只走 settings（DPAPI 加密），日志里只出现账号，不出现密码；
  模板 URL 里的密码在日志中打码成 ****。
- 登录失败不连环重试：Guard 退避 30s → 60s → 120s，最多 300s。
- 「门户要求验证码」这类只能人工过，识别到就停在 need_manual，让用户点开认证页，
  而不是每 30 秒刷一次把账号刷锁定。
"""

from __future__ import annotations

import hashlib
import base64
import ipaddress
import json
import logging
import re
import threading
import time
import uuid
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from . import hotspot

log = logging.getLogger("campus")

PROBE_204 = "http://connect.rom.miui.com/generate_204"
PROBE_TEXT = "http://www.msftconnecttest.com/connecttest.txt"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VPNTray-campus/1.0"

PROVIDERS = ("auto", "drcom", "srun", "eportal", "portal", "custom")
PROVIDER_TEXT = {
    "auto": "自动识别",
    "drcom": "Dr.COM（172.32 那套）",
    "srun": "深澜 srun",
    "eportal": "华为 / H3C ePortal",
    "portal": "通用表单（抓页面自动填）",
    "custom": "自定义登录地址",
}

MIN_INTERVAL = 15
MAX_INTERVAL = 600
BACKOFF_STEPS = (30, 60, 120, 300)

_STATE: dict[str, Any] = {
    "online": None,          # None = 还没探过
    "portal": "",
    "kind": "",
    "provider": "",
    "user": "",
    "last_try": 0.0,
    "last_ok": 0.0,
    "tries": 0,
    "error": "",
    "need_manual": False,
    "detail": "",
}
_LOCK = threading.RLock()


# ────────────────────────── 本机信息 ──────────────────────────

def local_ip() -> str:
    try:
        return hotspot.local_ip()
    except Exception:  # noqa: BLE001
        return ""


def gateway() -> str:
    try:
        return hotspot.default_gateway()
    except Exception:  # noqa: BLE001
        return ""


def mac_address() -> str:
    """取本机 MAC 给 Dr.COM 的参数用（00:11:22:33:44:55 → 001122334455）。"""
    try:
        n = uuid.getnode()
        if (n >> 40) & 0x01:      # 组播位 = getnode() 没拿到真实网卡
            return ""
        return "".join(f"{(n >> s) & 0xFF:02X}" for s in range(40, -8, -8))
    except Exception:  # noqa: BLE001
        return ""


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return s


# ────────────────────────── 探测 ──────────────────────────

def _looks_like_portal(url: str, body: str) -> bool:
    if url and not any(h in url for h in ("generate_204", "connecttest")):
        return True
    low = (body or "")[:4000].lower()
    for kw in ("password", "登录", "login", "认证", "eportal", "srun", "drcom"):
        if kw in low:
            return True
    return False


def detect_kind(url: str, body: str = "") -> str:
    """按地址和页面特征判门户类型。判不出就 portal（通用表单）。"""
    u = (url or "").lower()
    b = (body or "").lower()
    if "drcom" in u or "drcom" in b or "城市热点" in (body or ""):
        return "drcom"
    if "srun" in u or "srun" in b or "深澜" in (body or ""):
        return "srun"
    if "eportal" in u or "eportal" in b or "portal.do" in u:
        return "eportal"
    return "portal"


def discover(timeout: float = 4.0) -> dict[str, Any]:
    """探一次：通不通、认证页地址、类型。

    返回 {online, portal, kind, detail}。online=False 且 portal 为空时多半是
    真断网（连不上网关），不是门户。
    """
    out = {"online": False, "portal": "", "kind": "", "detail": ""}
    try:
        with _session() as s:
            try:
                r = s.get(PROBE_204, timeout=timeout, allow_redirects=False)
                if r.status_code == 204:
                    out["online"] = True
                    out["detail"] = "204 正常"
                    return out
                loc = r.headers.get("Location") or ""
                body = ""
                if r.status_code < 400 and r.text:
                    body = r.text[:6000]
                if loc or _looks_like_portal(loc, body):
                    out["portal"] = loc or r.url
                    out["kind"] = detect_kind(out["portal"], body)
                    out["detail"] = f"HTTP {r.status_code} → {out['portal'][:120]}"
                    return out
                out["detail"] = f"HTTP {r.status_code}（内容不符）"
            except Exception as e:  # noqa: BLE001
                out["detail"] = f"204 探测失败 {type(e).__name__}"
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"会话异常 {type(e).__name__}"
    return out


def verify(timeout: float = 3.0) -> bool:
    """登录后复验：只认 204。"""
    try:
        with _session() as s:
            r = s.get(PROBE_204, timeout=timeout, allow_redirects=False)
            return r.status_code == 204
    except Exception:  # noqa: BLE001
        return False


# ────────────────────────── 表单解析 ──────────────────────────

_FORM_RE = re.compile(r"<form\b[^>]*>(.*?)</form>", re.I | re.S)
_ACTION_RE = re.compile(r"""action\s*=\s*["']([^"']*)["']""", re.I)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")

_USER_HINTS = ("username", "user_name", "user", "account", "loginname", "login_name",
               "name", "userid", "user_id", "studentid", "student_id", "uid", "email")
_PASS_HINTS = ("password", "passwd", "pwd", "pass", "userpass", "user_password")
_CAPTCHA_HINTS = ("captcha", "vcode", "verifycode", "verify_code", "authcode",
                  "checkcode", "code", "rand")


def _attrs(tag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag):
        k = (m.group(1) or "").lower()
        v = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        out[k] = v
    return out


def _pick(fields: list[dict[str, str]], hints: tuple[str, ...],
          want_pass: bool = False) -> str:
    # 密码字段按 type=password 认；账号字段绝不能走这条（否则账号被填成密码框）
    if want_pass:
        for f in fields:
            if (f.get("type") or "").lower() == "password":
                return f.get("name", "")
    # 先精确匹配，再做包含匹配（避免 user 抢到 username2 之类）
    for h in hints:
        for f in fields:
            if (f.get("name") or "").lower() == h:
                return f.get("name", "")
    for h in hints:
        for f in fields:
            if h in (f.get("name") or "").lower():
                return f.get("name", "")
    return ""


def parse_form(html: str, page_url: str) -> dict[str, Any] | None:
    """从门户页里挑出「登录表单」，返回 {action, method, fields, user_field, pass_field, captcha}。

    挑法：优先带 password 输入的 form；没有再挑 action 里带 login 的。
    多个表单时按出现顺序取第一个命中的 —— 认证页通常登录表单在最前面。
    """
    best: dict[str, Any] | None = None
    for m in _FORM_RE.finditer(html or ""):
        block = m.group(1)
        raw = m.group(0)
        fields = []
        has_pass = False
        for tag in _INPUT_RE.findall(block):
            a = _attrs(tag)
            name = a.get("name") or a.get("id") or ""
            if not name:
                continue
            t = (a.get("type") or "text").lower()
            if t in ("password", "hidden", "text", "tel", "email", "number", "submit"):
                fields.append({"name": name, "type": t, "value": a.get("value", "")})
            if t == "password":
                has_pass = True
        action_m = _ACTION_RE.search(raw)
        action = (action_m.group(1) if action_m else "") or page_url
        form = {
            "action": urljoin(page_url, action),
            "method": (re.search(r"""method\s*=\s*["'](\w+)["']""", raw, re.I) or [None, "post"])[1].lower()
            if re.search(r"""method\s*=\s*["'](\w+)["']""", raw, re.I) else "post",
            "fields": fields,
            "user_field": _pick(fields, _USER_HINTS),
            "pass_field": _pick(fields, _PASS_HINTS, want_pass=True),
            "captcha": any(any(h in (f["name"] or "").lower() for h in _CAPTCHA_HINTS)
                           for f in fields if (f.get("type") or "") != "hidden"),
        }
        score = (2 if has_pass else 0) + (1 if "login" in (action or "").lower() else 0)
        if best is None or score > best["score"]:
            form["score"] = score
            best = form
    if not best or (best["score"] == 0 and not best.get("user_field")):
        return None
    return best


# ────────────────────────── 各门户登录 ──────────────────────────

def _base(portal: str) -> str:
    p = urlparse(portal or "")
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _ip_str() -> tuple[str, str]:
    ip = local_ip()
    return ip, mac_address()


def _drcom_params(user: str, password: str) -> dict[str, str]:
    ip, mac = _ip_str()
    return {
        "callback": "dr1003",
        "login_method": "1",
        "user_account": f",0,{user}",
        "user_password": password,
        "wlan_user_ip": ip,
        "wlan_user_mac": mac,
        "wlan_ac_ip": "",
        "wlan_ac_name": "",
        "jsVersion": "4.1",
        "v": str(int(time.time() * 1000) % 10000),
    }


def _parse_drcom(text: str) -> tuple[bool, str]:
    """Dr.COM 回的是 dr1003({"result":1,"msg":"..."}) 或纯 JSON。"""
    body = (text or "").strip()
    m = re.search(r"\{.*\}", body, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            code = data.get("result", data.get("code", data.get("ret_code")))
            msg = str(data.get("msg") or data.get("message") or "")
            if str(code) in ("1", "0", "ok", "success"):
                return True, msg or "认证成功"
            return False, msg or f"返回码 {code}"
        except Exception:  # noqa: BLE001
            pass
    if "success" in body.lower() or "成功" in body:
        return True, body[:120]
    return False, body[:160] or "空响应"


def _login_drcom(base: str, user: str, password: str, timeout: float) -> dict[str, Any]:
    url = base + "/drcom/login"
    try:
        with _session() as s:
            r = s.get(url, params=_drcom_params(user, password), timeout=timeout,
                      allow_redirects=False)
            ok, msg = _parse_drcom(r.text or "")
            return {"ok": ok, "msg": msg, "code": r.status_code, "url": r.url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "code": 0, "url": url}


def _login_srun(base: str, user: str, password: str, timeout: float) -> dict[str, Any]:
    """深澜：/cgi-bin/srun_portal。不同版本对 password 的编码不同，
    这里给最通行的 {BASE64}，失败时让人用 custom 模板兜底。"""
    ip = local_ip()
    pw = base64.b64encode(password.encode("utf-8")).decode("ascii")
    data = {
        "action": "login",
        "username": user,
        "password": "{" + "BASE64" + "}" + pw,
        "ac_id": "1",
        "ip": ip,
        "nas_ip": "",
        "user_mac": mac_address(),
        "save_me": "1",
        "double_stack": "0",
    }
    url = base + "/cgi-bin/srun_portal"
    try:
        with _session() as s:
            r = s.post(url, data=data, timeout=timeout, allow_redirects=False)
            ok, msg = _parse_drcom(r.text or "")
            return {"ok": ok, "msg": msg, "code": r.status_code, "url": r.url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "code": 0, "url": url}


def _login_eportal(base: str, user: str, password: str, timeout: float) -> dict[str, Any]:
    """华为 / H3C ePortal：两段式。先 user/pwd 加密后的 query 登录，
    老版本吃明文 user/pass。两种都发一次，谁回 result=1 算谁。"""
    ip, mac = _ip_str()
    url = base + "/eportal/portal/login"
    tries = []
    common = {"callback": "dr1003", "login_method": "1", "wlan_user_ip": ip,
              "wlan_user_mac": mac, "wlan_ac_ip": "", "wlan_ac_name": ""}
    tries.append({**common, "user_account": f",0,{user}", "user_password": password})
    tries.append({**common, "user_account": user, "user_password": password})
    last = {"ok": False, "msg": "", "code": 0, "url": url}
    for params in tries:
        try:
            with _session() as s:
                r = s.get(url, params=params, timeout=timeout, allow_redirects=False)
                ok, msg = _parse_drcom(r.text or "")
                last = {"ok": ok, "msg": msg, "code": r.status_code, "url": r.url}
                if ok:
                    return last
        except Exception as e:  # noqa: BLE001
            last = {"ok": False, "msg": f"{type(e).__name__}: {e}", "code": 0, "url": url}
    return last


def _login_portal(portal: str, user: str, password: str, timeout: float) -> dict[str, Any]:
    """通用表单：抓门户页 → 找登录表单 → 填 → 提交。"""
    page = portal
    try:
        with _session() as s:
            r = s.get(portal, timeout=timeout, allow_redirects=True)
            html = r.text or ""
            form = parse_form(html, r.url or portal)
            if not form:
                return {"ok": False, "msg": "门户页里没找到登录表单（可能需要人机验证，改用自定义模板）",
                        "code": r.status_code, "url": portal}
            if form.get("captcha"):
                return {"ok": False, "need_manual": True,
                        "msg": "门户要求验证码，需要先手动登录一次（点「打开认证页」）",
                        "code": r.status_code, "url": form["action"]}
            if not form["user_field"] or not form["pass_field"]:
                return {"ok": False,
                        "msg": "表单字段认不出来（账号字段 %r / 密码字段 %r）"
                               % (form["user_field"], form["pass_field"]),
                        "code": r.status_code, "url": form["action"]}
            payload = {f["name"]: f.get("value", "") for f in form["fields"]}
            payload[form["user_field"]] = user
            payload[form["pass_field"]] = password
            method = form.get("method") or "post"
            if method == "get":
                resp = s.get(form["action"], params=payload, timeout=timeout, allow_redirects=True)
            else:
                resp = s.post(form["action"], data=payload, timeout=timeout, allow_redirects=True)
            return {"ok": True, "msg": f"已提交表单（HTTP {resp.status_code}），正在复验",
                    "code": resp.status_code, "url": form["action"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "code": 0, "url": page}


def _mask(text: str) -> str:
    """日志/返回里把密码打掉。"""
    return re.sub(r"(user_password|password|pwd|pass)=([^&\s]+)", r"\1=****", text or "", flags=re.I)


def _login_custom(template: str, portal: str, user: str, password: str,
                  timeout: float) -> dict[str, Any]:
    ip, mac = _ip_str()
    url = (template or "").replace("{user}", user).replace("{pass}", password) \
        .replace("{ip}", ip).replace("{mac}", mac).replace("{portal}", portal or "")
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "msg": "自定义地址要以 http:// 或 https:// 开头", "code": 0, "url": url}
    try:
        with _session() as s:
            r = s.get(url, timeout=timeout, allow_redirects=True)
            ok, msg = _parse_drcom(r.text or "")
            return {"ok": ok, "msg": msg or f"HTTP {r.status_code}", "code": r.status_code,
                    "url": _mask(r.url)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "code": 0, "url": _mask(url)}


def login(provider: str = "auto", portal: str = "", user: str = "", password: str = "",
          template: str = "", timeout: float = 6.0) -> dict[str, Any]:
    """登一次，并且立刻复验。返回 {ok, msg, kind, portal, provider, need_manual, ...}。

    调用方（app.campus_login）负责把结果写进 state 并通知；
    这里只做「发出请求 + 判结果」，方便单测。
    """
    provider = (provider or "auto").strip().lower()
    if provider not in PROVIDERS:
        provider = "auto"
    if not user or not password:
        return {"ok": False, "msg": "还没填账号或密码", "provider": provider,
                "portal": portal, "kind": "", "need_manual": False}
    base = _base(portal)
    if provider == "custom" and not template:
        return {"ok": False, "msg": "选了自定义登录地址，但模板是空的", "provider": provider,
                "portal": portal, "kind": "", "need_manual": False}

    kind = detect_kind(portal) if portal else ""
    use = provider
    if use == "auto":
        if template:
            use = "custom"
        elif kind in ("drcom", "srun", "eportal"):
            use = kind
        else:
            use = "portal"
    if not base and use != "custom":
        return {"ok": False, "msg": "还没认出认证页地址（可以先在浏览器打开认证页，"
                                    "或手填「认证页地址」）", "provider": use,
                "portal": portal, "kind": kind, "need_manual": False}

    if use == "drcom":
        res = _login_drcom(base, user, password, timeout)
    elif use == "srun":
        res = _login_srun(base, user, password, timeout)
    elif use == "eportal":
        res = _login_eportal(base, user, password, timeout)
    elif use == "custom":
        res = _login_custom(template, portal, user, password, timeout)
    else:
        res = _login_portal(portal, user, password, timeout)

    res.update({"provider": use, "portal": portal, "kind": kind})
    res.setdefault("need_manual", False)
    if res.get("ok"):
        res["ok"] = verify()
        if not res["ok"]:
            res["msg"] = (res.get("msg") or "") + "；复验仍未通过（204 不通）"
        res["msg"] = _mask(res.get("msg") or "")
    with _LOCK:
        _STATE.update({
            "portal": portal or _STATE.get("portal") or "",
            "kind": kind or _STATE.get("kind") or "",
            "provider": use,
            "user": user,
            "last_try": time.time(),
            "tries": int(_STATE.get("tries") or 0) + 1,
            "need_manual": bool(res.get("need_manual")),
            "error": "" if res.get("ok") else str(res.get("msg") or ""),
            "detail": _mask(str(res.get("msg") or "")),
        })
        if res.get("ok"):
            _STATE["last_ok"] = time.time()
            _STATE["online"] = True
            _STATE["tries"] = 0
    return res


# ────────────────────────── 守护 ──────────────────────────

class Guard:
    """掉线自动登。只在「被门户劫持」时动手，真断网不动（登也登不上）。"""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._stop = threading.Event()
        self._th: threading.Thread | None = None
        self._fail = 0

    # ---- 配置 ----
    def _cfg(self, key: str, default: Any = "") -> Any:
        try:
            return self.app.settings.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    def _interval(self) -> int:
        try:
            v = int(self._cfg("campus_interval", 30) or 30)
        except Exception:  # noqa: BLE001
            v = 30
        return max(MIN_INTERVAL, min(MAX_INTERVAL, v))

    def _next_gap(self) -> int:
        if self._fail <= 0:
            return self._interval()
        return BACKOFF_STEPS[min(self._fail, len(BACKOFF_STEPS)) - 1]

    # ---- 循环 ----
    def start(self) -> bool:
        if self._th and self._th.is_alive():
            return True
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="campus-guard", daemon=True)
        self._th.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        th = self._th
        if th and th.is_alive():
            th.join(timeout=3)

    def _loop(self) -> None:
        # 启动时先等 8 秒：内核 / 系统代理 / 订阅测速都在抢网络，
        # 这时候探 204 必然超时，会把好网络误判成「要登录」。
        if self._stop.wait(8):
            return
        while not self._stop.is_set():
            try:
                if self._cfg("campus_enabled", False):
                    self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("校园网守护异常: %s", e)
            if self._stop.wait(self._next_gap()):
                return

    def tick(self) -> dict[str, Any]:
        """一轮：探 → 需要就登 → 登不上退避并提示。返回本轮状态（给测试用）。"""
        info = discover()
        with _LOCK:
            _STATE["online"] = info["online"]
            if info["portal"]:
                _STATE["portal"] = info["portal"]
            if info["kind"]:
                _STATE["kind"] = info["kind"]
            _STATE["detail"] = info["detail"]
        if info["online"]:
            self._fail = 0
            return {"action": "none", "online": True, "detail": info["detail"]}

        if not info["portal"]:
            # 真断网：连门户都没劫持到，登也白登
            self._fail += 1
            log.info("校园网：链路未通（%s），本轮不尝试登录", info["detail"])
            return {"action": "offline", "online": False, "detail": info["detail"]}

        user = str(self._cfg("campus_user", "") or "")
        password = str(self._cfg("campus_pass", "") or "")
        if not user or not password:
            with _LOCK:
                _STATE["need_manual"] = True
                _STATE["error"] = "没填校园网账号或密码"
            return {"action": "need_config", "online": False, "portal": info["portal"]}

        provider = str(self._cfg("campus_provider", "auto") or "auto")
        template = str(self._cfg("campus_template", "") or "")
        before_gw = gateway()
        res = login(provider, info["portal"], user, password, template)
        if res.get("ok"):
            self._fail = 0
            log.info("校园网已登录（%s，门户 %s）", res.get("provider"), info["portal"])
            self._recover(before_gw)
            return {"action": "login_ok", "online": True, "result": res}
        self._fail += 1
        log.warning("校园网登录失败（第 %d 次）：%s", self._fail, res.get("msg"))
        return {"action": "login_fail", "online": False, "result": res}

    def _recover(self, before_gw: str) -> None:
        """登录成功后把「因为没过认证而让路」的东西接回来。"""
        app = self.app
        try:
            if hasattr(app, "on_campus_online"):
                app.on_campus_online(before_gw)
        except Exception as e:  # noqa: BLE001
            log.warning("校园网登录后续动作失败: %s", e)


_GUARD: Guard | None = None


def start(app: Any) -> bool:
    global _GUARD
    if _GUARD is None:
        _GUARD = Guard(app)
    return _GUARD.start()


def stop() -> None:
    global _GUARD
    if _GUARD is not None:
        _GUARD.stop()


def status() -> dict[str, Any]:
    with _LOCK:
        out = dict(_STATE)
    out["enabled"] = bool(_cfg_of_app("campus_enabled", False))
    out["interval"] = int(_cfg_of_app("campus_interval", 30) or 30)
    out["provider_cfg"] = str(_cfg_of_app("campus_provider", "auto") or "auto")
    out["user_cfg"] = str(_cfg_of_app("campus_user", "") or "")
    out["providers"] = [{"key": k, "text": PROVIDER_TEXT[k]} for k in PROVIDERS]
    return out


def _cfg_of_app(key: str, default: Any = "") -> Any:
    g = _GUARD
    if g is not None:
        return g._cfg(key, default)
    return default


__all__ = ["discover", "detect_kind", "parse_form", "login", "verify", "status",
           "start", "stop", "Guard", "PROVIDERS", "PROVIDER_TEXT", "PROBE_204",
           "local_ip", "gateway", "mac_address", "MIN_INTERVAL", "MAX_INTERVAL"]
