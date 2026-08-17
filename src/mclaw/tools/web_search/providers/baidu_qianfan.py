"""百度千帆 (Baidu Qianfan) 联网搜索 provider — 国内免费额度.

API: ``POST https://qianfan.baidubce.com/v2/ai_search/web_search``
鉴权：``Authorization: Bearer <AppBuilder API Key>``
Docs: https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy

免费额度：100 次/天（约 3000 次/月）。
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


class BaiduQianfanProvider:
    id = "baidu_qianfan"
    label = "百度千帆"
    requires_credential = True
    auto_detect_order = 12
    signup_url = "https://console.bce.baidu.com/ai_apaas/appbuilder"
    docs_url = "https://ai.baidu.com/ai-doc/AppBuilder/pmaxd1hvy"

    _ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"

    def is_available(self) -> bool:
        return bool((settings.baidu_qianfan_api_key or "").strip())

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        region: str = "wt-wt",  # 千帆不需要 region；保持签名一致
        safesearch: str = "moderate",
        timeout_seconds: float = 0.0,
    ) -> list[SearchResult]:
        api_key = (settings.baidu_qianfan_api_key or "").strip()
        if not api_key:
            raise MissingCredentialError(
                "BAIDU_QIANFAN_API_KEY not configured", provider_id=self.id
            )

        payload = {
            "messages": [{"role": "user", "content": query}],
            "resource_type_filter": [
                {
                    "type": "web",
                    "top_k": min(max(1, max_results), 50),
                }
            ],
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
                f"baidu_qianfan transport failure: {describe_httpx_failure(exc)}",
                provider_id=self.id,
            ) from exc
        except httpx.HTTPError as exc:
            raise NetworkUnreachableError(
                f"baidu_qianfan HTTP error: {describe_httpx_failure(exc)}",
                provider_id=self.id,
            ) from exc

        if resp.status_code in (401, 403):
            raise AuthFailedError(
                f"baidu_qianfan rejected credential (HTTP {resp.status_code})",
                provider_id=self.id,
            )
        if resp.status_code == 429:
            raise RateLimitedError(
                "baidu_qianfan rate-limited (HTTP 429)", provider_id=self.id
            )
        if resp.status_code >= 400:
            raise NetworkUnreachableError(
                f"baidu_qianfan HTTP {resp.status_code}: {resp.text[:200]}",
                provider_id=self.id,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise NetworkUnreachableError(
                "baidu_qianfan returned non-JSON response", provider_id=self.id
            ) from exc

        return _extract_baidu_results(data)

    async def news_search(self, *args: Any, **kwargs: Any) -> list[SearchResult] | None:
        # 千帆 web_search 没有独立 news 端点；返回 None 让 runtime 换下一家
        return None


def _extract_baidu_results(data: dict) -> list[SearchResult]:
    """解析千帆 web_search 响应（结果在顶层 ``references`` 数组）。"""
    err_code = data.get("error_code") or data.get("code")
    if err_code is not None:
        msg = data.get("error_msg") or data.get("message") or f"code={err_code}"
        raise NetworkUnreachableError(
            f"baidu_qianfan api error: {msg}", provider_id="baidu_qianfan"
        )

    refs = data.get("references") or []
    out: list[SearchResult] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        out.append(
            SearchResult(
                title=str(item.get("title") or "无标题"),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or item.get("snippet") or ""),
                source=str(item.get("web_anchor") or item.get("website") or ""),
                date=str(item.get("date") or ""),
            )
        )
    return out


register(BaiduQianfanProvider())
