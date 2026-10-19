"""mihomo RESTful API 客户端（本机回环，不走系统代理）。"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("api")


class ApiError(RuntimeError):
    pass


class MihomoAPI:
    def __init__(self, host: str = "127.0.0.1", port: int = 9091, secret: str = "") -> None:
        self.base = f"http://{host}:{port}"
        self.secret = secret
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.headers.update({"Content-Type": "application/json"})

    # ——— 基础 ———
    def _req(self, method: str, path: str, timeout: float = 3.0, **kw) -> Any:
        headers = dict(self.s.headers)
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        try:
            r = self.s.request(method, self.base + path, headers=headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            raise ApiError(f"{method} {path} 连接失败: {e}") from e
        if r.status_code >= 400:
            raise ApiError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    def alive(self, timeout: float = 1.0) -> bool:
        try:
            self._req("GET", "/version", timeout=timeout)
            return True
        except ApiError:
            return False

    def version(self) -> dict:
        return self._req("GET", "/version") or {}

    # ——— 代理 ———
    def proxies(self) -> dict:
        return (self._req("GET", "/proxies") or {}).get("proxies", {})

    def select(self, group: str, name: str) -> None:
        self._req("PUT", f"/proxies/{_q(group)}", json={"name": name}, timeout=5)

    def unselect(self, group: str) -> None:
        self._req("DELETE", f"/proxies/{_q(group)}", timeout=5)

    def delay(self, name: str, url: str, timeout_ms: int = 5000) -> int | None:
        try:
            r = self._req(
                "GET",
                f"/proxies/{_q(name)}/delay",
                params={"url": url, "timeout": timeout_ms},
                timeout=timeout_ms / 1000 + 3,
            )
            return int(r.get("delay")) if isinstance(r, dict) and r.get("delay") else None
        except ApiError:
            return None

    def group_delay(self, group: str, url: str, timeout_ms: int = 5000) -> dict[str, int]:
        try:
            r = self._req(
                "GET",
                f"/group/{_q(group)}/delay",
                params={"url": url, "timeout": timeout_ms},
                timeout=timeout_ms / 1000 + 8,
            )
        except ApiError as e:
            log.warning("组测速失败 %s: %s", group, e)
            return {}
        if not isinstance(r, dict):
            # 内核回了非 JSON（代理劫持、404 HTML 等），.items() 会抛 AttributeError，
            # 而外层只 catch ApiError，异常会一路炸到测速线程。
            log.warning("组测速响应格式异常: %r", str(r)[:120])
            return {}
        out: dict[str, int] = {}
        for k, v in r.items():
            if isinstance(v, int) and v > 0:
                out[k] = v
        return out

    # ——— 全局 ———
    def configs(self) -> dict:
        return self._req("GET", "/configs") or {}

    def set_mode(self, mode: str) -> None:
        self._req("PATCH", "/configs", json={"mode": mode}, timeout=5)

    def reload(self, path: str) -> None:
        self._req("PUT", "/configs", params={"force": "true"}, json={"path": path}, timeout=15)

    def connections(self) -> dict:
        return self._req("GET", "/connections", timeout=4) or {}

    def close_connection(self, cid: str) -> None:
        self._req("DELETE", f"/connections/{_q(str(cid))}", timeout=5)

    def close_all_connections(self) -> None:
        self._req("DELETE", "/connections", timeout=5)


def _q(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")
