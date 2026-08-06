"""Phase 4a tests — daily-brief renderer + pipeline digest writer."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finpulse_pipeline import run_daily_brief
from finpulse_report.render import (
    DigestStats,
    build_daily_brief,
    render_html,
    render_markdown,
    select_articles,
    session_label,
    _band,
)
from finpulse_task_manager import FinpulseTaskManager


def _article(
    *,
    idx: int,
    score: float | None,
    source: str = "wallstreetcn",
    title: str | None = None,
    url: str | None = None,
    fetched_at: str = "2025-01-01T08:00:00Z",
) -> dict:
    return {
        "id": f"a{idx}",
        "source_id": source,
        "url": url or f"https://example.com/a{idx}",
        "title": title or f"Headline number {idx}",
        "summary": "snippet",
        "published_at": "2025-01-01T07:00:00Z",
        "fetched_at": fetched_at,
        "ai_score": score,
        "ai_tags": [],
        "raw": {},
    }


class TestBandClassifier:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (9.5, "critical"),
            (9.0, "critical"),
            (8.9, "important"),
            (7.0, "important"),
            (6.0, "routine"),
            (5.0, "routine"),
            (4.0, "low"),
            (3.0, "low"),
            (2.0, "noise"),
            (None, "unscored"),
        ],
    )
    def test_thresholds(self, score: float | None, expected: str) -> None:
        assert _band(score) == expected


class TestSelectArticles:
    def test_ranks_by_score_then_fetched_at(self) -> None:
        items = [
            _article(idx=1, score=5.0, fetched_at="2025-01-01T08:00:00Z"),
            _article(idx=2, score=8.0, fetched_at="2025-01-01T07:00:00Z"),
            _article(idx=3, score=None, fetched_at="2025-01-01T09:00:00Z"),
            _article(idx=4, score=8.0, fetched_at="2025-01-01T09:00:00Z"),
        ]
        picked, stats = select_articles(items, top_k=3)
        assert [a["id"] for a in picked] == ["a4", "a2", "a1"]
        assert stats.total_scanned == 4
        assert stats.total_selected == 3
        assert stats.by_source == {"wallstreetcn": 3}
        assert stats.score_bands.get("important", 0) == 2
        assert stats.score_bands.get("routine", 0) == 1

    def test_top_k_clamped(self) -> None:
        items = [_article(idx=i, score=float(i)) for i in range(5)]
        picked, _ = select_articles(items, top_k=0)
        assert len(picked) == 1
        picked, _ = select_articles(items, top_k=999)
        assert len(picked) == 5

    def test_empty(self) -> None:
        picked, stats = select_articles([], top_k=10)
        assert picked == []
        assert stats.total_scanned == 0
        assert stats.total_selected == 0


class TestRenderMarkdown:
    def test_includes_score_source_and_url(self) -> None:
        stats = DigestStats(total_scanned=3, total_selected=2)
        md = render_markdown(
            ctx=_ctx("morning"),
            articles=[_article(idx=1, score=9.1), _article(idx=2, score=5.0)],
            stats=stats,
        )
        assert "财经早报" in md
        assert "[wallstreetcn] [9.1]" in md
        assert "[wallstreetcn] [5.0]" in md
        assert "https://example.com/a1" in md
        assert "fin-pulse" in md or "财经脉动" in md

    def test_empty_articles_placeholder(self) -> None:
        stats = DigestStats()
        md = render_markdown(ctx=_ctx("evening"), articles=[], stats=stats)
        assert "暂无命中资讯" in md


class TestRenderHtml:
    def test_contains_theme_variables_and_escapes(self) -> None:
        stats = DigestStats(total_scanned=1, total_selected=1)
        html_blob = render_html(
            ctx=_ctx("noon"),
            articles=[
                _article(
                    idx=1,
                    score=9.0,
                    title="<script>alert(1)</script>",
                    url="https://ex.com/x?y=1&z=2",
                )
            ],
            stats=stats,
        )
        assert "data-theme" in html_blob
        assert "<!doctype html>" in html_blob
        assert "&lt;script&gt;" in html_blob
        assert "alert(1)" in html_blob
        assert "<script>alert(1)</script>" not in html_blob
        assert "y=1&amp;z=2" in html_blob
        assert "score-critical" in html_blob

    def test_empty_shows_placeholder(self) -> None:
        html_blob = render_html(
            ctx=_ctx("morning"), articles=[], stats=DigestStats()
        )
        assert "暂无命中资讯" in html_blob


class TestBuildDailyBrief:
    def test_produces_both_blobs(self) -> None:
        items = [_article(idx=i, score=float(10 - i)) for i in range(5)]
        md, html_blob, stats = build_daily_brief(
            items, session="morning", top_k=3, lang="zh"
        )
        assert "财经早报" in md
        assert stats.total_selected == 3
        assert "<!doctype html>" in html_blob

    def test_english(self) -> None:
        md, _, _ = build_daily_brief([], session="noon", lang="en")
        assert "Midday Brief" in md
        assert "No articles" in md


class TestSessionLabel:
    @pytest.mark.parametrize(
        "session,lang,expected",
        [
            ("morning", "zh", "财经早报"),
            ("noon", "zh", "财经午报"),
            ("evening", "zh", "财经晚报"),
            ("morning", "en", "Morning Brief"),
            ("noon", "en", "Midday Brief"),
            ("evening", "en", "Evening Brief"),
        ],
    )
    def test_labels(self, session: str, lang: str, expected: str) -> None:
        assert session_label(session, lang=lang) == expected


# ── Pipeline integration ─────────────────────────────────────────────


async def _make_tm(tmp_path: Path) -> FinpulseTaskManager:
    tm = FinpulseTaskManager(tmp_path / "fp.db")
    await tm.init()
    return tm


async def _seed(tm: FinpulseTaskManager, count: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(count):
        aid, _ = await tm.upsert_article(
            source_id="wallstreetcn",
            url=f"https://ex.com/{i}",
            url_hash=f"h{i}",
            title=f"Article {i}",
            fetched_at=now,
            summary="s",
            content="c",
            published_at=now,
            raw={},
        )
        await tm.update_article_ai(
            aid,
            ai_score=float(10 - i % 10),
            ai_tags=[{"name": "tag", "lang": "zh"}],
        )


def test_run_daily_brief_writes_digest_row(tmp_path: Path) -> None:
    async def _body() -> None:
        tm = await _make_tm(tmp_path)
        try:
            await _seed(tm, 5)
            result = await run_daily_brief(
                tm, session="morning", since_hours=72, top_k=3
            )
            assert result["ok"] is True
            digest_id = result["digest_id"]
            assert digest_id
            assert result["stats"]["total_selected"] == 3

            row = await tm.get_digest(digest_id)
            assert row is not None
            assert row["session"] == "morning"
            assert row["html_blob"].startswith("<!doctype html>")
            assert "财经早报" in row["markdown_blob"]
            assert row["stats"]["total_selected"] == 3
        finally:
            await tm.close()

    asyncio.run(_body())


def test_run_daily_brief_rejects_bad_session(tmp_path: Path) -> None:
    async def _body() -> None:
        tm = await _make_tm(tmp_path)
        try:
            with pytest.raises(ValueError):
                await run_daily_brief(tm, session="bogus")
        finally:
            await tm.close()

    asyncio.run(_body())


def test_run_daily_brief_empty_universe_still_writes_digest(tmp_path: Path) -> None:
    async def _body() -> None:
        tm = await _make_tm(tmp_path)
        try:
            result = await run_daily_brief(tm, session="evening", since_hours=24)
            assert result["ok"] is True
            row = await tm.get_digest(result["digest_id"])
            assert row is not None
            assert "暂无命中资讯" in row["html_blob"]
        finally:
            await tm.close()

    asyncio.run(_body())


def test_run_daily_brief_marks_task_succeeded(tmp_path: Path) -> None:
    async def _body() -> None:
        tm = await _make_tm(tmp_path)
        try:
            task = await tm.create_task(
                mode="daily_brief",
                params={"session": "morning"},
                status="running",
            )
            await _seed(tm, 2)
            await run_daily_brief(
                tm,
                session="morning",
                since_hours=48,
                top_k=5,
                task_id=task["id"],
            )
            fresh = await tm.get_task(task["id"])
            assert fresh is not None
            assert fresh["status"] == "succeeded"
            assert fresh["progress"] == 1.0
        finally:
            await tm.close()

    asyncio.run(_body())


def _ctx(session: str, lang: str = "zh"):
    from finpulse_report.render import DigestContext

    return DigestContext(
        session=session, lang=lang, generated_at="2025-01-01T08:30:00Z"
    )
