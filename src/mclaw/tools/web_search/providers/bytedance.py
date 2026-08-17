"""火山引擎·豆包搜索 (Byted Web Search) provider — 国内免费额度.

API: ``POST https://open.feedcoopapi.com/search_api/web_search``
Console/API Key: https://console.volcengine.com/search-infinity/api-key
Docs: https://www.volcengine.com/docs/85508/1650263

免费额度：个人 500 次/月（次月 1 日重置）。
"""

from __future__ import annotations

import logging
from typing import Any

from ....config import settings
from ..base import (
    AuthFailedError,
    MissingCredentialError,
    NetworkUnreachableError,
    RateLimitedError,
    SearchResult,
)
from ..registry import register
from ._http import describe_httpx_failure, search_httpx_client_kwargs

logger = logging.getLogger(__name__)


class BytedanceProvider:
    id = "bytedance"
    label = "豆包搜索 (火山引擎)"
    requires_credential = True
    auto_detect_order = 14
    signup_url = "https://console.volcengine.com/search-infinity/api-key"
    docs_url = "https://www.volcengine.com/docs/85508/1650263"

    _ENDPOINT = "https://open.feedcoopapi.com/search_api/web_search"

    def is_available(self) -> bool:
        return bool((settings.bytedance_search_api_key or "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        region: str = "wt-wt",  # 火山引擎搜索不需要 region；保持签名一致
        safesearch: str = "moderate",
        timeout_seconds: float = 0.0,
    ) -> list[SearchResult]:
        api_key = (settings.bytedance_search_api_key or "").strip()
        if not api_key:
            raise MissingCredentialError(
                "BYTEDANCE_SEARCH_API_KEY not configured", provider_id=self.id
            )

        payload = {
            "query": query,
            "count": min(max(1, max_results), 50),
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = timeout_seconds if timeout_seconds and timeout_seconds > 0 else 30.0

        import httpx

        try:
            async with httpx.AsyncClient(
                **search_httpx_client_kwargs(timeout=timeout, target_url=self._ENDPOINT)
            ) as client:
                resp = await client.post(self._ENDPOINT, headers=headers, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            raise NetworkUnreachableError(
                f"bytedance transport failure: {describe_httpx_failure(exc)}",
                provider_id=self.id,
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkUnreachableError(
                f"bytedance HTTP error: {describe_httpx_failure(exc)}",
                provider_id=self.id,
            ) from exc

        if resp.status_code in (401, 403):
            raise AuthFailedError(
                f"bytedance rejected credential (HTTP {resp.status_code})",
                provider_id=self.id,
            )
        if resp.status_code == 429:
            raise RateLimitedError("bytedance rate-limited (HTTP 429)", provider_id=self.id)
        if resp.status_code >= 400:
            raise NetworkUnreachableError(
                f"bytedance HTTP {resp.status_code}: {resp.text[:200]}",
                provider_id=self.id,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise NetworkUnreachableError(
                "bytedance returned non-JSON response", provider_id=self.id
            ) from exc

        return _extract_bytedance_results(data)

    async def news_search(self, *args: Any, **kwargs: Any) -> list[SearchResult] | None:
        # 火山引擎联网搜索没有独立 news 端点；返回 None 让 runtime 换下一家
        return None


def _extract_bytedance_results(data: dict) -> list[SearchResult]:
    """解析火山引擎 web_search 响应（防御式兼容多种字段形状）。

    成功：``code == 10000``，结果在 ``data.webPages.list``。
    错误码：10403 凭证无效 / 700429 限流 / 10406 等配额耗尽。
    """
    code = data.get("code")
    if code is not None:
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = -1
        if code_int != 10000:
            msg = data.get("msg") or data.get("message") or f"code={code}"
            if code_int in (10403, 10411):
                raise AuthFailedError(f"bytedance auth failed: {msg}", provider_id="bytedance")
            if code_int == 700429:
                raise RateLimitedError(f"bytedance rate-limited: {msg}", provider_id="bytedance")
            raise NetworkUnreachableError(
                f"bytedance api error: {msg}", provider_id="bytedance"
            )

    items: list[dict] = []
    data_obj = data.get("data") or {}
    if isinstance(data_obj, dict):
        web_pages = data_obj.get("webPages") or {}
        if isinstance(web_pages, dict):
            items = web_pages.get("list") or web_pages.get("value") or []
    if not items:
        items = data.get("results") or []

    out: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append(
            SearchResult(
                title=str(item.get("title") or "无标题"),
                url=str(item.get("url") or ""),
                snippet=str(
                    item.get("summary") or item.get("snippet") or item.get("content") or ""
                ),
                source=str(item.get("siteName") or ""),
                date=str(item.get("time") or item.get("date") or ""),
            )
        )
    return out


register(BytedanceProvider())
