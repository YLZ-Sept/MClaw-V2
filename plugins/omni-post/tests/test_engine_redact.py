"""Sanity tests for the log / screenshot redaction helpers.

These don't drive a real browser; they just assert that the regex we
ship will actually scrub cookies out of a debug log the user might
paste into a bug report.
"""

from __future__ import annotations

import pytest
from omni_post_engine_pw import redact_cookie_text


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        (
            "Cookie: sessionid=deadbeef1234; Other=ok",
            "deadbeef1234",
        ),
        (
            "Set-Cookie: csrftoken=abc.def.ghi; Path=/",
            "abc.def.ghi",
        ),
        (
            "authorization: Bearer eyJhbGciOi.payload.signature",
            "eyJhbGciOi.payload.signature",
        ),
        (
            "token=MY-SUPER-SECRET xyz",
            "MY-SUPER-SECRET",
        ),
    ],
)
def test_redact_cookie_text_strips_sensitive(raw: str, must_not_contain: str) -> None:
    out = redact_cookie_text(raw)
    assert "[REDACTED]" in out
    assert must_not_contain not in out


def test_redact_leaves_innocent_text_alone() -> None:
    raw = "User-Agent: Mozilla/5.0 — nothing sensitive here"
    assert redact_cookie_text(raw) == raw
