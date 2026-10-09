"""Persistence tests for :class:`WebAccessConfig`.

Covers the Stage 1 hardening of ``web_access.json``:

- ``_save()`` is atomic: a stale ``*.tmp`` from a previous crash never wins.
- ``_save()`` calls ``fsync`` so contents survive power loss.
- ``_load()`` treats a corrupt JSON file as "regenerate" rather than crashing.
- Concurrent writers do not interleave their bytes thanks to the per-instance
  lock + atomic ``os.replace``.
"""

from __future__ import annotations

import json
import os
import threading
from unittest import mock

import pytest

from mclaw.api.auth import WebAccessConfig


def _read(path):
    return json.loads(path.read_text("utf-8"))


def test_save_writes_atomically_via_replace(tmp_path):
    cfg = WebAccessConfig(tmp_path)
    cfg.add_user("admin", "hunter22", role="admin")

    web_file = tmp_path / "web_access.json"
    tmp_file = tmp_path / "web_access.tmp"

    assert web_file.exists()
    assert not tmp_file.exists(), "tmp file should be removed after atomic replace"

    data = _read(web_file)
    # v2（多用户）格式：密码哈希落在 users.<name> 下，顶层 password_hash /
    # password_user_set 已不再落盘（后者退化为内存属性）。
    assert data["users"]["admin"]["password_hash"], "password hash should be persisted"
    assert data["users"]["admin"]["role"] == "admin"


def test_save_calls_fsync_on_file(tmp_path):
    cfg = WebAccessConfig(tmp_path)

    with mock.patch("os.fsync") as fsync_spy:
        cfg.add_user("admin", "hunter22", role="admin")

    assert fsync_spy.called, "fsync must be called to flush bytes to disk"


@pytest.mark.skipif(os.name != "posix", reason="dir fsync is POSIX-only")
def test_save_fsyncs_parent_dir_on_posix(tmp_path):
    cfg = WebAccessConfig(tmp_path)

    fsynced_fds: list[int] = []
    real_fsync = os.fsync

    def _spy(fd):
        fsynced_fds.append(fd)
        return real_fsync(fd)

    with mock.patch("os.fsync", side_effect=_spy):
        cfg.add_user("admin", "hunter22", role="admin")

    assert len(fsynced_fds) >= 2, "should fsync at least twice: payload file + parent directory"


def test_load_recovers_from_corrupt_file(tmp_path, caplog):
    cfg = WebAccessConfig(tmp_path)
    cfg.add_user("admin", "hunter22", role="admin")

    web_file = tmp_path / "web_access.json"
    web_file.write_text("{ not valid json ::", encoding="utf-8")

    with caplog.at_level("ERROR"):
        cfg2 = WebAccessConfig(tmp_path)

    assert any(
        "corrupted" in rec.message or "corrupt" in rec.message.lower() for rec in caplog.records
    ), "corrupt-file recovery should log at ERROR level"

    assert cfg2.jwt_secret, "fresh config should have a new jwt_secret"
    assert not cfg2.verify_password("admin", "hunter22"), (
        "corrupt file means password is gone — user must re-setup"
    )


def test_load_recovers_from_empty_file(tmp_path):
    web_file = tmp_path / "web_access.json"
    web_file.write_text("", encoding="utf-8")

    cfg = WebAccessConfig(tmp_path)
    assert cfg.jwt_secret


def test_load_recovers_from_truncated_file(tmp_path):
    cfg = WebAccessConfig(tmp_path)
    cfg.add_user("admin", "hunter22", role="admin")

    web_file = tmp_path / "web_access.json"
    contents = web_file.read_text("utf-8")
    web_file.write_text(contents[: len(contents) // 2], encoding="utf-8")

    cfg2 = WebAccessConfig(tmp_path)
    assert cfg2.jwt_secret
    assert not cfg2.verify_password("admin", "hunter22")


def test_concurrent_saves_do_not_corrupt_file(tmp_path):
    cfg = WebAccessConfig(tmp_path)
    cfg.add_user("admin", "initial-pw", role="admin")  # change_password 要求用户已存在
    errors: list[BaseException] = []

    def worker(pw: str) -> None:
        try:
            for _ in range(8):
                cfg.change_password("admin", pw)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"pass{i:02d}word",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent writes raised: {errors!r}"

    data = _read(tmp_path / "web_access.json")
    assert data["users"]["admin"]["password_hash"], "file must still be valid JSON"


def test_save_survives_when_tmp_exists_from_previous_crash(tmp_path):
    """A leftover ``*.tmp`` from a previous crash must not block a fresh save."""
    cfg = WebAccessConfig(tmp_path)

    stale_tmp = tmp_path / "web_access.tmp"
    stale_tmp.write_text("garbage from previous crash", encoding="utf-8")

    cfg.add_user("admin", "hunter22", role="admin")

    assert cfg.verify_password("admin", "hunter22")
    assert not stale_tmp.exists(), "stale tmp should be replaced (os.replace clobbers)"
