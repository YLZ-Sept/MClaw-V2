"""Unit tests for omni_post_assets.UploadPipeline (no ffmpeg required)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from omni_post_assets import UploadPipeline
from omni_post_task_manager import OmniPostTaskManager


@pytest.fixture()
async def upload_pipeline(tmp_path: Path):
    tm = OmniPostTaskManager(tmp_path / "t.db")
    await tm.init()
    pipeline = UploadPipeline(
        uploads_dir=tmp_path / "uploads",
        thumbs_dir=tmp_path / "thumbs",
        task_manager=tm,
        chunk_bytes=16,
    )
    try:
        yield pipeline, tm
    finally:
        await tm.close()


@pytest.mark.asyncio()
async def test_single_chunk_upload(upload_pipeline) -> None:
    pipeline, tm = upload_pipeline
    data = b"hello world"
    init = await pipeline.init_upload(
        filename="hello.mp4",
        filesize=len(data),
        kind="video",
    )
    upload_id = init["upload_id"]
    assert init["total_chunks"] == 1

    pipeline.write_chunk(upload_id=upload_id, chunk_index=0, payload=data)
    result = await pipeline.finalize(upload_id=upload_id)
    assert result["deduped"] is False
    assert result["md5"] == hashlib.md5(data).hexdigest()  # noqa: S324
    asset = await tm.get_asset(result["asset_id"])
    assert asset is not None
    assert asset["kind"] == "video"


@pytest.mark.asyncio()
async def test_multi_chunk_upload_assembly(upload_pipeline) -> None:
    pipeline, _tm = upload_pipeline
    data = b"A" * 16 + b"B" * 16 + b"C" * 5
    init = await pipeline.init_upload(filename="f.mp4", filesize=len(data), kind="video")
    upload_id = init["upload_id"]
    chunks = [data[i : i + 16] for i in range(0, len(data), 16)]
    for i, c in enumerate(chunks):
        pipeline.write_chunk(upload_id=upload_id, chunk_index=i, payload=c)
    result = await pipeline.finalize(upload_id=upload_id)
    final = Path(result["storage_path"])
    assert final.read_bytes() == data
    assert result["md5"] == hashlib.md5(data).hexdigest()  # noqa: S324


@pytest.mark.asyncio()
async def test_dedup_second_upload(upload_pipeline) -> None:
    pipeline, _tm = upload_pipeline
    data = b"dedup-me"
    init1 = await pipeline.init_upload(filename="a.mp4", filesize=len(data), kind="video")
    pipeline.write_chunk(upload_id=init1["upload_id"], chunk_index=0, payload=data)
    r1 = await pipeline.finalize(upload_id=init1["upload_id"])

    init2 = await pipeline.init_upload(filename="b.mp4", filesize=len(data), kind="video")
    pipeline.write_chunk(upload_id=init2["upload_id"], chunk_index=0, payload=data)
    r2 = await pipeline.finalize(upload_id=init2["upload_id"])

    assert r1["asset_id"] == r2["asset_id"]
    assert r2["deduped"] is True


@pytest.mark.asyncio()
async def test_init_short_circuits_on_md5_hint(upload_pipeline) -> None:
    """If the client already knows the md5 and it's in the table, init
    MUST return ``deduped: True`` without opening a new session."""

    pipeline, _tm = upload_pipeline
    data = b"short-circuit-me"
    first = await pipeline.init_upload(filename="x.mp4", filesize=len(data), kind="video")
    pipeline.write_chunk(upload_id=first["upload_id"], chunk_index=0, payload=data)
    r1 = await pipeline.finalize(upload_id=first["upload_id"])
    expected_md5 = r1["md5"]

    second = await pipeline.init_upload(
        filename="y.mp4",
        filesize=len(data),
        kind="video",
        md5_hint=expected_md5,
    )
    assert second.get("deduped") is True
    assert second["asset_id"] == r1["asset_id"]
    assert "upload_id" not in second


@pytest.mark.asyncio()
async def test_reject_unknown_upload(upload_pipeline) -> None:
    pipeline, _tm = upload_pipeline
    with pytest.raises(KeyError):
        pipeline.write_chunk(upload_id="no-such", chunk_index=0, payload=b"")
    with pytest.raises(KeyError):
        await pipeline.finalize(upload_id="no-such")
