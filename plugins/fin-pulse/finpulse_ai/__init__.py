"""AI-layer package for fin-pulse — Phase 3+.

V1.0 currently only exports the URL-merge + simhash helpers from
:mod:`finpulse_ai.dedupe`. The filter (tag extraction + scoring) and
thematic-cluster paths land in Phase 3, keyed off
``config['dedupe.use_llm']`` / ``config['ai_interests']``.
"""

from __future__ import annotations

from finpulse_ai.dedupe import (
    canonical_dedupe_key,
    simhash_title,
    simhash_distance,
    group_by_canonical_url,
    group_by_simhash,
)
from finpulse_ai.filter import (
    extract_tags,
    interests_digest,
    score_batch,
)
from finpulse_ai.rules_suggest import suggest_rules_text

__all__ = [
    "canonical_dedupe_key",
    "extract_tags",
    "group_by_canonical_url",
    "group_by_simhash",
    "interests_digest",
    "score_batch",
    "simhash_distance",
    "simhash_title",
    "suggest_rules_text",
]
