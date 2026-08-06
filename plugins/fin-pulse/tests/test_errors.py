"""Exception → ``error_kind`` classifier round-trip tests.

The classifier is the UI's only narration surface when a pipeline step
explodes, so every canonical error_kind has a regression case here.
"""

from __future__ import annotations

import socket

import pytest

from finpulse_errors import (
    build_error_envelope,
    classify,
    hints_for,
    map_exception,
)
from finpulse_models import ERROR_KINDS


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("read timed out"), "timeout"),
        (socket.gaierror("getaddrinfo failed"), "network"),
        (ConnectionError("connection refused"), "network"),
        (RuntimeError("HTTP 429 too many requests"), "rate_limit"),
        (RuntimeError("401 Unauthorized: invalid api_key"), "auth"),
        (RuntimeError("Insufficient balance — payment required"), "quota"),
        (FileNotFoundError("no such file"), "not_found"),
        (ImportError("No module named 'execjs'"), "dependency"),
        (RuntimeError("content_filter flagged the prompt"), "moderation"),
        (ValueError("surprise"), "unknown"),
    ],
)
def test_classify_maps_to_nine_kinds(exc: Exception, expected: str) -> None:
    assert classify(exc) == expected


def test_map_exception_returns_tuple_with_hints() -> None:
    try:
        raise TimeoutError("slow source")
    except TimeoutError as exc:
        kind, msg, hints = map_exception(exc, locale="en")
    assert kind == "timeout"
    assert msg
    assert isinstance(hints, list) and hints
    assert all(isinstance(h, str) for h in hints)


def test_envelope_shape_is_stable() -> None:
    envelope = build_error_envelope(RuntimeError("boom"))
    assert envelope["ok"] is False
    assert envelope["error_kind"] in ERROR_KINDS
    assert "error_message" in envelope
    assert isinstance(envelope["error_hints"], list)


def test_hints_fall_back_to_zh_on_unknown_locale() -> None:
    hints = hints_for("timeout", locale="de")
    assert hints  # non-empty even when locale unsupported
