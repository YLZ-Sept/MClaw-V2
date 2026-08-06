"""Unit tests for omni_post_cookies.CookiePool."""

from __future__ import annotations

from pathlib import Path

import pytest
from omni_post_cookies import CookieEncryptError, CookiePool


def test_seal_and_open_roundtrip(tmp_path: Path) -> None:
    pool = CookiePool(tmp_path)
    cipher = pool.seal("raw-cookie-string")
    assert isinstance(cipher, bytes)
    assert b"raw-cookie-string" not in cipher
    plain = pool.open(cipher)
    assert plain == "raw-cookie-string"


def test_seal_rejects_empty(tmp_path: Path) -> None:
    pool = CookiePool(tmp_path)
    with pytest.raises(ValueError):
        pool.seal("")


def test_open_rejects_foreign_cipher(tmp_path: Path) -> None:
    pool_a = CookiePool(tmp_path / "a")
    pool_b = CookiePool(tmp_path / "b")
    cipher = pool_a.seal("hello")
    with pytest.raises(CookieEncryptError):
        pool_b.open(cipher)


def test_salt_file_is_stable(tmp_path: Path) -> None:
    pool = CookiePool(tmp_path)
    key_path = tmp_path / "identity.salt"
    assert key_path.exists()
    cipher = pool.seal("x")

    # New pool pointing at the same dir should read the same salt.
    pool2 = CookiePool(tmp_path)
    assert pool2.open(cipher) == "x"


@pytest.mark.asyncio()
async def test_probe_lazy_returns_cookie_expired_on_decrypt_failure(
    tmp_path: Path,
) -> None:
    pool = CookiePool(tmp_path)
    account = {"id": "acc_1", "cookie_cipher": b"not-a-valid-fernet"}

    async def never_called(_: str) -> str:
        raise AssertionError("probe_fn must not run when decryption fails")

    verdict = await pool.probe_lazy(account, probe_fn=never_called)
    assert verdict == "cookie_expired"


@pytest.mark.asyncio()
async def test_probe_lazy_returns_unknown_when_probe_raises(tmp_path: Path) -> None:
    pool = CookiePool(tmp_path)
    cipher = pool.seal("cookie-plaintext")
    account = {"id": "acc_1", "cookie_cipher": cipher}

    async def boom(_: str) -> str:
        raise RuntimeError("simulated network blip")

    verdict = await pool.probe_lazy(account, probe_fn=boom)
    assert verdict == "unknown"


@pytest.mark.asyncio()
async def test_probe_lazy_passes_plaintext_to_probe(tmp_path: Path) -> None:
    pool = CookiePool(tmp_path)
    cipher = pool.seal("the-real-cookie")
    account = {"id": "acc_2", "cookie_cipher": cipher}

    captured: list[str] = []

    async def probe(plaintext: str) -> str:
        captured.append(plaintext)
        return "ok"

    assert await pool.probe_lazy(account, probe_fn=probe) == "ok"
    assert captured == ["the-real-cookie"]
