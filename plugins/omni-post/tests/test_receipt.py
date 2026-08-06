"""Publish-receipt asset contract tests.

These pin the *semantic* shape of the ``publish_receipt`` asset that
`omni-post` publishes on the host Asset Bus, as registered in
``docs/asset-kinds.md``. Downstream plugins (fin-pulse / idea-research /
MDRM / comment-hub) program against this shape, so silent drifts are
bugs — not code-style fluctuations. Keep these tests strict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from omni_post_pipeline import (
    PUBLISH_RECEIPT_SCHEMA_VERSION,
    PUBLISH_RECEIPT_TTL_SECONDS,
    PipelineDeps,
    _build_receipt_payload,
    _publish_receipt_asset,
)


@dataclass
class _FakeOutcome:
    success: bool = True
    published_url: str | None = "https://www.douyin.com/video/7xxxxx"
    screenshots: list[str] = field(default_factory=lambda: ["/tmp/a.png", "/tmp/b.png"])
    duration_ms: int = 45_123
    upload_ms: int = 22_000
    metrics: dict[str, Any] = field(
        default_factory=lambda: {"submit_ms": 12, "was_auto_submit": True}
    )


class _FakeTaskManager:
    def __init__(self, account: dict[str, Any] | None) -> None:
        self._account = account

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        return self._account


class _FakeAPI:
    """Stubs the two host methods the receipt helper touches."""

    def __init__(self, *, should_raise: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._should_raise = should_raise

    async def publish_asset(self, **kwargs: Any) -> str | None:
        self.calls.append(kwargs)
        if self._should_raise:
            raise RuntimeError("simulated bus failure")
        return "ast-bus-1"


def _task(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "tk-1",
        "platform": "douyin",
        "account_id": "acc-1",
        "asset_id": "ast-local-1",
        "engine": "pw",
        "retry_count": 0,
    }
    base.update(overrides)
    return base


def _account(nickname: str | None = "alice") -> dict[str, Any]:
    return {"id": "acc-1", "nickname": nickname, "platform": "douyin"}


# ---------------------------------------------------------------------------
# Pure payload builder
# ---------------------------------------------------------------------------


def test_receipt_success_payload_has_all_documented_keys() -> None:
    payload = _build_receipt_payload(
        task=_task(),
        account=_account(),
        outcome=_FakeOutcome(),
        status="succeeded",
        error_kind=None,
        retries=0,
        screenshots=["/tmp/a.png"],
        metrics={"duration_ms": 45_123, "upload_ms": 22_000},
    )
    expected_keys = {
        "schema_version",
        "task_id",
        "asset_id",
        "platform",
        "account_id",
        "account_nickname",
        "status",
        "error_kind",
        "published_url",
        "published_at",
        "engine",
        "retry_count",
        "screenshot_path",
        "metrics",
    }
    assert expected_keys.issubset(payload.keys()), (
        f"missing keys: {expected_keys - set(payload.keys())}"
    )
    assert payload["schema_version"] == PUBLISH_RECEIPT_SCHEMA_VERSION == 1
    assert payload["status"] == "succeeded"
    assert payload["error_kind"] is None
    assert payload["platform"] == "douyin"
    assert payload["published_url"] == "https://www.douyin.com/video/7xxxxx"
    assert payload["screenshot_path"] == "/tmp/a.png"
    assert payload["engine"] == "pw"


def test_receipt_failure_payload_carries_error_kind_and_no_url() -> None:
    payload = _build_receipt_payload(
        task=_task(),
        account=_account(),
        outcome=None,
        status="failed",
        error_kind="cookie_expired",
        retries=2,
        screenshots=None,
        metrics=None,
    )
    assert payload["status"] == "failed"
    assert payload["error_kind"] == "cookie_expired"
    assert payload["published_url"] is None
    assert payload["screenshot_path"] is None
    assert payload["retry_count"] == 2
    assert payload["metrics"] == {}


def test_receipt_payload_skips_account_when_missing() -> None:
    payload = _build_receipt_payload(
        task=_task(account_id="acc-ghost"),
        account=None,
        outcome=_FakeOutcome(),
        status="succeeded",
        error_kind=None,
        retries=0,
        screenshots=[],
        metrics={},
    )
    assert payload["account_id"] == "acc-ghost"
    assert payload["account_nickname"] is None


# ---------------------------------------------------------------------------
# End-to-end: JSON file + bus publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_receipt_writes_file_and_calls_bus(tmp_path: Path) -> None:
    receipts_dir = tmp_path / "receipts"
    api = _FakeAPI()
    deps = PipelineDeps(
        task_manager=_FakeTaskManager(_account("alice")),
        cookie_pool=None,
        engine=None,  # type: ignore[arg-type]
        selectors_dir=tmp_path,
        screenshot_dir=tmp_path,
        settings={},
        api=api,
        receipts_dir=receipts_dir,
    )
    await _publish_receipt_asset(
        deps,
        _task(),
        outcome=_FakeOutcome(),
        status="succeeded",
        error_kind=None,
        retries=0,
        screenshots=["/tmp/a.png"],
    )

    # 1. File on disk is well-formed JSON with the full payload.
    written = receipts_dir / "tk-1.json"
    assert written.is_file(), "receipt file should be created"
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk["task_id"] == "tk-1"
    assert on_disk["status"] == "succeeded"
    assert on_disk["metrics"]["duration_ms"] == 45_123

    # 2. Bus publish saw the same metadata + pointed at the file.
    assert len(api.calls) == 1
    call = api.calls[0]
    assert call["asset_kind"] == "publish_receipt"
    assert call["source_path"] == str(written)
    assert call["shared_with"] == ["*"]
    assert call["ttl_seconds"] == PUBLISH_RECEIPT_TTL_SECONDS
    assert call["metadata"]["schema_version"] == 1
    assert call["metadata"]["account_nickname"] == "alice"


@pytest.mark.asyncio
async def test_publish_receipt_survives_bus_failure(tmp_path: Path) -> None:
    """Bus errors must not propagate — they are logged and swallowed.

    The forensic JSON on disk must still land so an ops person can
    backfill later.
    """

    receipts_dir = tmp_path / "receipts"
    api = _FakeAPI(should_raise=True)
    deps = PipelineDeps(
        task_manager=_FakeTaskManager(_account()),
        cookie_pool=None,
        engine=None,  # type: ignore[arg-type]
        selectors_dir=tmp_path,
        screenshot_dir=tmp_path,
        settings={},
        api=api,
        receipts_dir=receipts_dir,
    )
    await _publish_receipt_asset(
        deps,
        _task(),
        outcome=_FakeOutcome(),
        status="succeeded",
        error_kind=None,
        retries=0,
        screenshots=[],
    )
    assert (receipts_dir / "tk-1.json").is_file()


@pytest.mark.asyncio
async def test_publish_receipt_tolerates_missing_api(tmp_path: Path) -> None:
    receipts_dir = tmp_path / "receipts"
    deps = PipelineDeps(
        task_manager=_FakeTaskManager(_account()),
        cookie_pool=None,
        engine=None,  # type: ignore[arg-type]
        selectors_dir=tmp_path,
        screenshot_dir=tmp_path,
        settings={},
        api=None,
        receipts_dir=receipts_dir,
    )
    await _publish_receipt_asset(
        deps,
        _task(),
        outcome=None,
        status="failed",
        error_kind="network",
        retries=3,
        screenshots=None,
    )
    on_disk = json.loads((receipts_dir / "tk-1.json").read_text(encoding="utf-8"))
    assert on_disk["status"] == "failed"
    assert on_disk["error_kind"] == "network"
    assert on_disk["retry_count"] == 3
