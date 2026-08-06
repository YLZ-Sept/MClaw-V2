"""Digest renderer — turns scored articles into markdown + HTML blobs.

Split from the pipeline module so the template strings stay isolated
and the render path can be unit-tested without touching SQLite.
"""

from __future__ import annotations

from finpulse_report.render import (
    DigestContext,
    DigestStats,
    build_daily_brief,
    render_html,
    render_markdown,
    select_articles,
)

__all__ = [
    "DigestContext",
    "DigestStats",
    "build_daily_brief",
    "render_html",
    "render_markdown",
    "select_articles",
]
