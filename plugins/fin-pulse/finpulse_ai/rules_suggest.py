"""AI-assisted rule drafting for the Radar tab.

Accepts a user's plain-language description ("我想追踪美联储的政策动向，
以及特朗普对中国关税的任何最新表态，但排除小道消息和论坛贴文") and
returns a fin-pulse rules_text suitable for
``POST /radar/evaluate``. The function is intentionally tolerant: any
Brain failure falls back to a deterministic heuristic so the UI always
gets *something* useful even when the host has no LLM configured.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from finpulse_ai.prompts import (
    RULES_SUGGEST_SYSTEM_EN,
    RULES_SUGGEST_SYSTEM_ZH,
    RULES_SUGGEST_USER_TEMPLATE,
)

logger = logging.getLogger(__name__)

_MAX_DESC_CHARS = 2000
_MAX_OUT_CHARS = 4000


def _brain_content(response: Any) -> str:
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    content = getattr(response, "content", None)
    if content is None and isinstance(response, dict):
        content = response.get("content")
    return str(content or "")


def _strip_fence(raw: str) -> str:
    """Strip a surrounding ``` fence / prose preface if present."""
    if not raw:
        return ""
    fence = re.search(r"```[a-zA-Z]*\s*([\s\S]*?)```", raw)
    if fence:
        return fence.group(1).strip()
    return raw.strip()


def _fallback_rules(description: str) -> str:
    """Deterministic fallback used when the Brain is offline / missing.

    We tokenize by commas / whitespace and emit a single ``+word`` group
    per non-trivial token. It is intentionally conservative — the user
    will always be able to hand-edit the result.
    """

    tokens = [tok for tok in re.split(r"[\s,，、;；]+", description.strip()) if tok]
    keywords: list[str] = []
    for tok in tokens:
        if len(tok) < 2 or tok.lower() in {"我想", "追踪", "监控", "关于", "关于的", "the", "and"}:
            continue
        if tok.startswith(("不要", "排除", "忽略", "除外")):
            rest = tok[2:].strip()
            if rest:
                keywords.append(f"!{rest}")
        else:
            keywords.append(f"+{tok}")
        if len(keywords) >= 8:
            break
    if not keywords:
        return ""
    return "\n".join(keywords) + "\n"


async def suggest_rules_text(
    brain: Any,
    *,
    description: str,
    existing: str = "",
    lang: str = "zh",
    max_tokens: int = 400,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Return ``{"ok", "rules_text", "source"}``.

    ``source`` is ``"mclaw_llm"`` when the configured Mclaw model
    produced the text. Facade failures are returned as errors; no private
    provider or heuristic text fallback is used.
    """

    desc = (description or "").strip()
    if not desc:
        return {"ok": False, "error": "description is required"}
    if len(desc) > _MAX_DESC_CHARS:
        desc = desc[:_MAX_DESC_CHARS]
    existing_text = (existing or "").strip()
    if len(existing_text) > _MAX_OUT_CHARS:
        existing_text = existing_text[:_MAX_OUT_CHARS]

    system = RULES_SUGGEST_SYSTEM_ZH if lang == "zh" else RULES_SUGGEST_SYSTEM_EN
    user = RULES_SUGGEST_USER_TEMPLATE.format(
        description=desc, existing=existing_text or "(无 / none)"
    )
    try:
        from mclaw.plugins.llm_support import complete_text

        response = await complete_text(
            brain,
            endpoint=str(brain.get_config().get("llm_endpoint") or ""),
            prompt=user,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        logger.warning("suggest_rules_text Plugin LLM failed: %s", exc)
        return {"ok": False, "error": str(exc), "source": "mclaw_llm"}

    raw = str(response.text or "")
    cleaned = _strip_fence(raw)
    if not cleaned:
        return {"ok": False, "error": "plugin_llm_empty_response"}
    if len(cleaned) > _MAX_OUT_CHARS:
        cleaned = cleaned[:_MAX_OUT_CHARS]
    return {"ok": True, "rules_text": cleaned, "source": "mclaw_llm"}


__all__ = ["suggest_rules_text"]
