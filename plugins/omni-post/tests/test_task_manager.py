"""Unit tests for OmniPostTaskManager — uses a temp sqlite db."""

from __future__ import annotations

from pathlib import Path

import pytest
from omni_post_task_manager import OmniPostTaskManager


@pytest.fixture()
async def tm(tmp_path: Path) -> OmniPostTaskManager:
    mgr = OmniPostTaskManager(tmp_path / "test.db")
    await mgr.init()
    try:
        yield mgr
    finally:
        await mgr.close()


@pytest.mark.asyncio()
async def test_create_and_get_task(tm: OmniPostTaskManager) -> None:
    task_id = await tm.create_task(
        platform="douyin",
        account_id="acc_1",
        asset_id="ast_1",
        payload={"title": "hi"},
        client_trace_id="trace-1",
    )
    row = await tm.get_task(task_id)
    assert row is not None
    assert row["status"] == "pending"
    assert row["payload"] == {"title": "hi"}


@pytest.mark.asyncio()
async def test_create_task_is_idempotent(tm: OmniPostTaskManager) -> None:
    """Same client_trace_id must resolve to the same task id."""

    t1 = await tm.create_task(
        platform="douyin",
        account_id="acc_1",
        asset_id="ast_1",
        payload={"title": "hi"},
        client_trace_id="trace-X",
    )
    t2 = await tm.create_task(
        platform="douyin",
        account_id="acc_1",
        asset_id="ast_1",
        payload={"title": "hi"},
        client_trace_id="trace-X",
    )
    assert t1 == t2


@pytest.mark.asyncio()
async def test_update_task_safe_whitelist(tm: OmniPostTaskManager) -> None:
    tid = await tm.create_task(
        platform="douyin",
        account_id="a",
        asset_id="b",
        payload={},
        client_trace_id="t1",
    )
    await tm.update_task_safe(tid, {"status": "succeeded"})
    row = await tm.get_task(tid)
    assert row["status"] == "succeeded"

    with pytest.raises(ValueError):
        await tm.update_task_safe(tid, {"created_at": "x"})
    with pytest.raises(ValueError):
        await tm.update_task_safe(tid, {"status": "not-a-real-status"})


@pytest.mark.asyncio()
async def test_asset_dedup_by_md5(tm: OmniPostTaskManager) -> None:
    a1 = await tm.create_asset(
        kind="video",
        filename="a.mp4",
        filesize=10,
        md5="deadbeef",
        storage_path="/tmp/a.mp4",
    )
    a2 = await tm.create_asset(
        kind="video",
        filename="b.mp4",  # different name, same md5
        filesize=10,
        md5="deadbeef",
        storage_path="/tmp/b.mp4",
    )
    assert a1 == a2


@pytest.mark.asyncio()
async def test_account_crud(tm: OmniPostTaskManager) -> None:
    aid = await tm.create_account(
        platform="douyin",
        nickname="alice",
        cookie_cipher=b"cipher",
        tags=["main"],
    )
    acc = await tm.get_account(aid)
    assert acc is not None
    assert acc["nickname"] == "alice"
    assert acc["tags"] == ["main"]

    await tm.update_account_safe(aid, {"nickname": "alice2"})
    acc2 = await tm.get_account(aid)
    assert acc2["nickname"] == "alice2"


@pytest.mark.asyncio()
async def test_platform_upsert_idempotent(tm: OmniPostTaskManager) -> None:
    await tm.upsert_platform(
        platform_id="douyin",
        display_name="抖音",
        supported_kinds=["video"],
    )
    await tm.upsert_platform(
        platform_id="douyin",
        display_name="Douyin",
        supported_kinds=["video", "dynamic"],
    )
    rows = await tm.list_platforms()
    douyin_rows = [r for r in rows if r["id"] == "douyin"]
    assert len(douyin_rows) == 1
    assert "dynamic" in douyin_rows[0]["supported_kinds"]


@pytest.mark.asyncio()
async def test_publish_history_records(tm: OmniPostTaskManager) -> None:
    asset_id = await tm.create_asset(
        kind="video",
        filename="a.mp4",
        filesize=10,
        md5="cafef00d",
        storage_path="/tmp/a.mp4",
    )
    tid = await tm.create_task(
        platform="douyin",
        account_id="acc-x",
        asset_id=asset_id,
        payload={},
        client_trace_id="trace-z",
    )
    await tm.record_publish_history(
        asset_id=asset_id,
        task_id=tid,
        platform="douyin",
        account_id="acc-x",
        status="succeeded",
        published_url="https://douyin.com/video/1",
    )
    history = await tm.list_publish_history(asset_id=asset_id)
    assert len(history) == 1
    assert history[0]["status"] == "succeeded"


@pytest.mark.asyncio()
async def test_count_account_published(tm: OmniPostTaskManager) -> None:
    asset_id = await tm.create_asset(
        kind="video",
        filename="a.mp4",
        filesize=10,
        md5="ab",
        storage_path="/tmp/a.mp4",
    )
    tid = await tm.create_task(
        platform="douyin",
        account_id="acc-q",
        asset_id=asset_id,
        payload={},
        client_trace_id="trace-q",
    )
    await tm.record_publish_history(
        asset_id=asset_id,
        task_id=tid,
        platform="douyin",
        account_id="acc-q",
        status="succeeded",
    )
    count = await tm.count_account_published_since("acc-q", "1970-01-01T00:00:00Z")
    assert count == 1


@pytest.mark.asyncio()
async def test_selectors_health_upsert(tm: OmniPostTaskManager) -> None:
    await tm.upsert_selector_health(
        platform="douyin",
        hit_rate=1.0,
        total_probes=5,
        failed_probes=0,
    )
    await tm.upsert_selector_health(
        platform="douyin",
        hit_rate=0.6,
        total_probes=10,
        failed_probes=4,
        last_error="selector gone",
    )
    rows = await tm.list_selector_health()
    assert len(rows) == 1
    assert rows[0]["hit_rate"] == pytest.approx(0.6)
    assert rows[0]["last_error"] == "selector gone"
