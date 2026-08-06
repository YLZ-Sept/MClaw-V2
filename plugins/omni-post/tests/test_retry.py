"""Unit tests for the retry / backoff / auto_submit degradation path.

We stub the Playwright engine and cookie pool with tiny in-memory
fakes so the pipeline's control flow can be asserted without a real
browser. What we care about here is NOT DOM behaviour (covered by
integration tests in S4) but the state-machine contract: retry only
on retryable ErrorKinds, exponential-with-jitter backoff bounded at
60 s, auto_submit degrading after N failures, terminal success /
failure writes to the task row, and cookie-decrypt bail-out before
adapter construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from omni_post_cookies import CookieEncryptError, CookiePool
from omni_post_models import ErrorKind
from omni_post_pipeline import PipelineDeps, _jittered_backoff, run_publish_task
from omni_post_task_manager import OmniPostTaskManager


@dataclass
class _FakeOutcome:
    success: bool
    error_kind: str | None = None
    error_message: str = ""
    published_url: str | None = None
    screenshots: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


@dataclass
class _FakeEngine:
    """Scripted engine: each call to run_task returns the next outcome."""

    outcomes: list[_FakeOutcome]
    seen_auto_submit: list[bool] = field(default_factory=list)

    async def run_task(
        self,
        *,
        adapter,
        task,
        account,
        cookies_plaintext,
        asset_path,
        cover_path=None,
        settings=None,
    ) -> _FakeOutcome:
        merged = settings or {}
        self.seen_auto_submit.append(bool(merged.get("auto_submit", True)))
        if not self.outcomes:
            return _FakeOutcome(
                success=True,
                published_url="https://example.test/final",
            )
        return self.outcomes.pop(0)


class _FakeAdapter:
    platform_id = "douyin"
    bundle: dict = {}


async def _seed_task_and_account(tm: OmniPostTaskManager) -> str:
    account_id = await tm.create_account(
        platform="douyin",
        nickname="alice",
        cookie_cipher=b"ignored",
        daily_limit=5,
        weekly_limit=30,
        monthly_limit=100,
    )
    task_id = await tm.create_task(
        platform="douyin",
        account_id=account_id,
        asset_id=None,
        payload={"title": "t"},
    )
    return task_id


@pytest.fixture()
async def pipeline_deps(tmp_path: Path, monkeypatch):
    tm = OmniPostTaskManager(tmp_path / "t.db")
    await tm.init()
    pool = CookiePool(tmp_path / "vault")

    monkeypatch.setattr(pool, "open", lambda blob: "raw-cookie")

    def _fake_adapter(platform_id, selectors_dir):  # noqa: ARG001
        return _FakeAdapter()

    import omni_post_pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "build_adapter", _fake_adapter)

    try:
        yield tm, pool, pipeline_mod
    finally:
        await tm.close()


@pytest.mark.asyncio()
async def test_success_on_first_try_writes_succeeded(pipeline_deps) -> None:
    tm, pool, _pm = pipeline_deps
    task_id = await _seed_task_and_account(tm)
    engine = _FakeEngine(outcomes=[_FakeOutcome(success=True, published_url="https://ok")])
    deps = PipelineDeps(
        task_manager=tm,
        cookie_pool=pool,
        engine=engine,
        selectors_dir=Path("."),
        screenshot_dir=Path("."),
        settings={"retry_max_attempts": 3, "auto_submit_fail_threshold": 2},
    )
    await run_publish_task(deps, task_id)
    row = await tm.get_task(task_id)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["result_url"] == "https://ok"
    assert engine.seen_auto_submit == [True]


@pytest.mark.asyncio()
async def test_retry_on_network_then_succeeds(pipeline_deps) -> None:
    tm, pool, _pm = pipeline_deps
    task_id = await _seed_task_and_account(tm)
    engine = _FakeEngine(
        outcomes=[
            _FakeOutcome(success=False, error_kind=ErrorKind.NETWORK.value),
            _FakeOutcome(success=True, published_url="https://ok2"),
        ]
    )
    deps = PipelineDeps(
        task_manager=tm,
        cookie_pool=pool,
        engine=engine,
        selectors_dir=Path("."),
        screenshot_dir=Path("."),
        settings={
            "retry_max_attempts": 3,
            "auto_submit_fail_threshold": 99,
            "retry_backoff_base": 0.0,
        },
    )
    await run_publish_task(deps, task_id)
    row = await tm.get_task(task_id)
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["result_url"] == "https://ok2"


@pytest.mark.asyncio()
async def test_moderation_does_not_retry(pipeline_deps) -> None:
    tm, pool, _pm = pipeline_deps
    task_id = await _seed_task_and_account(tm)
    engine = _FakeEngine(
        outcomes=[
            _FakeOutcome(success=False, error_kind=ErrorKind.MODERATION.value),
            _FakeOutcome(success=True, published_url="https://never"),
        ]
    )
    deps = PipelineDeps(
        task_manager=tm,
        cookie_pool=pool,
        engine=engine,
        selectors_dir=Path("."),
        screenshot_dir=Path("."),
        settings={"retry_max_attempts": 3, "retry_backoff_base": 0.0},
    )
    await run_publish_task(deps, task_id)
    row = await tm.get_task(task_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_kind"] == ErrorKind.MODERATION.value
    assert len(engine.outcomes) == 1


@pytest.mark.asyncio()
async def test_auto_submit_degrades_after_threshold(pipeline_deps) -> None:
    tm, pool, _pm = pipeline_deps
    task_id = await _seed_task_and_account(tm)
    engine = _FakeEngine(
        outcomes=[
            _FakeOutcome(success=False, error_kind=ErrorKind.TIMEOUT.value),
            _FakeOutcome(success=False, error_kind=ErrorKind.TIMEOUT.value),
            _FakeOutcome(success=True, published_url="https://ok3"),
        ]
    )
    deps = PipelineDeps(
        task_manager=tm,
        cookie_pool=pool,
        engine=engine,
        selectors_dir=Path("."),
        screenshot_dir=Path("."),
        settings={
            "retry_max_attempts": 5,
            "auto_submit_fail_threshold": 2,
            "retry_backoff_base": 0.0,
        },
    )
    await run_publish_task(deps, task_id)
    assert engine.seen_auto_submit[:2] == [True, True]
    assert engine.seen_auto_submit[2] is False
    row = await tm.get_task(task_id)
    assert row is not None
    assert row["status"] == "succeeded"


def test_backoff_is_bounded() -> None:
    for attempt in range(0, 10):
        sleep = _jittered_backoff(2.0, attempt)
        assert 0.0 <= sleep <= 60.0


@pytest.mark.asyncio()
async def test_cookie_decrypt_failure_marks_cookie_expired(pipeline_deps, monkeypatch) -> None:
    tm, pool, _pm = pipeline_deps
    task_id = await _seed_task_and_account(tm)
    engine = _FakeEngine(outcomes=[_FakeOutcome(success=True)])

    def _boom(_blob):
        raise CookieEncryptError("simulated")

    monkeypatch.setattr(pool, "open", _boom)

    deps = PipelineDeps(
        task_manager=tm,
        cookie_pool=pool,
        engine=engine,
        selectors_dir=Path("."),
        screenshot_dir=Path("."),
        settings={"retry_max_attempts": 1, "retry_backoff_base": 0.0},
    )
    await run_publish_task(deps, task_id)
    row = await tm.get_task(task_id)
    assert row is not None
    assert row["status"] == "failed"
    assert row["error_kind"] == ErrorKind.COOKIE_EXPIRED.value
    assert engine.seen_auto_submit == []
