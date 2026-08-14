"""蝉镜 AI 开放平台服务端客户端。

- 凭证只从服务端环境变量 ``CHANJING_APP_ID`` / ``CHANJING_SECRET_KEY`` 读取，
  绝不出现在日志、注释或任何前端代码中。
- 业务成功以响应体 ``code == 0`` 为准，HTTP 200 不代表成功。
- ``access_token`` 在服务端缓存，并按 ``expire_in`` 提前一段时间刷新。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_URL = "https://open-api.chanjing.cc/open/v1"

# 提前刷新阈值（秒）：token 剩余有效期低于该值时重新获取
_TOKEN_REFRESH_MARGIN = 120

# 项目根目录（src/mclaw/integrations/chanjing/client.py 向上 4 级）
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


class ChanjingConfigError(RuntimeError):
    """凭证缺失 / 配置错误。"""


class ChanjingError(Exception):
    """蝉镜 API 业务错误（code != 0）。"""

    def __init__(self, path: str, code: int, msg: str, trace_id: str | None):
        self.path = path
        self.code = code
        self.msg = msg
        self.trace_id = trace_id
        super().__init__(f"[{path}] code={code} msg={msg} trace_id={trace_id}")


def load_credentials() -> tuple[str, str]:
    """读取蝉镜凭证（只读环境变量，不回显值）。

    依次加载项目根的 ``.env`` 与 ``.env.local``（两者均已 .gitignore），
    然后取 ``CHANJING_APP_ID`` / ``CHANJING_SECRET_KEY``。
    """
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    load_dotenv(_PROJECT_ROOT / ".env.local", override=False)
    app_id = os.environ.get("CHANJING_APP_ID", "").strip()
    secret_key = os.environ.get("CHANJING_SECRET_KEY", "").strip()
    if not app_id or not secret_key:
        raise ChanjingConfigError(
            "缺少蝉镜凭证：请设置服务端环境变量 CHANJING_APP_ID 与 CHANJING_SECRET_KEY"
        )
    return app_id, secret_key


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # epoch 秒


class ChanjingClient:
    """蝉镜开放平台客户端（async，httpx）。"""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        *,
        app_id: str | None = None,
        secret_key: str | None = None,
    ):
        self._base_url = base_url
        self._timeout = timeout
        self._app_id: str | None = app_id
        self._secret_key: str | None = secret_key
        self._token: _CachedToken | None = None
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ChanjingClient":
        if self._app_id is None or self._secret_key is None:
            self._app_id, self._secret_key = load_credentials()
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
        self._http = None

    # ---- 内部工具 ----

    async def _access_token(self) -> str:
        """获取（或复用未过期的）access_token。"""
        now = time.time()
        if self._token is not None and self._token.expires_at - now > _TOKEN_REFRESH_MARGIN:
            return self._token.value

        body = await self._request(
            "POST",
            "/access_token",
            json={"app_id": self._app_id, "secret_key": self._secret_key},
            auth=False,
        )
        data = body.get("data") or {}
        token = data.get("access_token")
        expire_in = int(data.get("expire_in") or 0)
        if not token:
            raise ChanjingError(
                "/access_token", body.get("code", 0), "access_token 为空", body.get("trace_id")
            )
        # data.expire_in 是绝对过期时间（epoch 秒），不是时长。
        # 实测返回约 1786763636（≈当前时间 + 27 小时），而非 56 年时长。
        if expire_in > 0:
            expires_at = float(expire_in)
        else:
            expires_at = now + 7200  # 兜底：未返回时按 2 小时有效期
        self._token = _CachedToken(value=token, expires_at=expires_at)
        return token

    async def _request(
        self, method: str, path: str, *, auth: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        assert self._http is not None
        headers = dict(kwargs.pop("headers", {}) or {})
        if auth:
            headers["access_token"] = await self._access_token()
        resp = await self._http.request(method, path, headers=headers, **kwargs)
        return self._parse(resp, path)

    def _parse(self, resp: httpx.Response, path: str) -> dict[str, Any]:
        try:
            body = resp.json()
        except Exception:
            raise ChanjingError(path, -1, f"非 JSON 响应 HTTP {resp.status_code}", None) from None
        code = body.get("code")
        if code != 0:
            raise ChanjingError(path, code, body.get("msg", ""), body.get("trace_id"))
        return body

    # ---- 业务接口 ----

    async def access_token(self) -> dict[str, Any]:
        """获取并缓存 access_token，返回 ``{"expire_in": int}``（不回显 token 值）。"""
        await self._access_token()
        expire_in = int(self._token.expires_at - time.time()) if self._token else 0
        return {"expire_in": expire_in}

    async def list_common_audio(self, page: int = 1, size: int = 20) -> dict[str, Any]:
        return await self._request("GET", "/list_common_audio", params={"page": page, "size": size})

    async def create_audio_task(
        self, audio_man: str, speed: float, text: str, plain_text: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/create_audio_task",
            json={
                "audio_man": audio_man,
                "speed": speed,
                "text": {"text": text, "plain_text": plain_text},
            },
        )

    async def audio_task_state(self, task_id: str) -> dict[str, Any]:
        return await self._request("POST", "/audio_task_state", json={"task_id": task_id})

    async def list_common_dp(self, page: int = 1, size: int = 20) -> dict[str, Any]:
        return await self._request("GET", "/list_common_dp", params={"page": page, "size": size})

    async def create_video(
        self,
        *,
        person: dict[str, Any],
        audio: dict[str, Any],
        screen_width: int,
        screen_height: int,
        **extra: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "person": person,
            "audio": audio,
            "screen_width": screen_width,
            "screen_height": screen_height,
        }
        payload.update(extra)
        return await self._request("POST", "/create_video", json=payload)

    async def get_video(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", "/video", params={"id": task_id})
