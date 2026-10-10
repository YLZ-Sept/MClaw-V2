"""The session-id derivation rule shared by every memory read/write path.

``sanitize_session_id`` replaced seven copy-pasted inline implementations, so
these cases pin the exact contract those call sites rely on: a raw session key
must collapse to the same string everywhere, or readers and writers would
address different rows.
"""

from types import SimpleNamespace

from mclaw.memory.session_identity import (
    DESKTOP_USER_ID,
    memory_session_id,
    sanitize_session_id,
)


def test_desktop_user_id_is_stable():
    assert DESKTOP_USER_ID == "desktop_user"


def test_colon_expands_to_double_underscore():
    assert sanitize_session_id("telegram:123:user") == "telegram__123__user"


def test_path_and_sql_unsafe_chars_collapse_to_single_underscore():
    assert sanitize_session_id("a/b\\c") == "a_b_c"
    assert sanitize_session_id("x*y?z") == "x_y_z"
    assert sanitize_session_id('q<w>e|r"t') == "q_w_e_r_t"
    assert sanitize_session_id("p%q+r=s") == "p_q_r_s"


def test_control_chars_are_stripped():
    assert sanitize_session_id("ctrl\x01char") == "ctrl_char"


def test_empty_and_none_yield_empty_string():
    assert sanitize_session_id("") == ""
    assert sanitize_session_id(None) == ""


def test_results_are_filesystem_safe():
    # No character that could escape a directory or break a filename survives.
    for raw in ("../etc/passwd", 'a"b', "c:d", "e\\f", "g\x00h"):
        safe = sanitize_session_id(raw)
        assert not set(safe) & set('/\\+=%?*<>|"\x00')


def test_idempotent_on_already_safe_ids():
    safe = sanitize_session_id("telegram__123__user")
    assert sanitize_session_id(safe) == safe


def test_memory_session_id_prefers_session_key():
    session = SimpleNamespace(session_key="telegram:123:user")
    assert memory_session_id(session) == "telegram__123__user"


def test_memory_session_id_falls_back_when_key_missing_or_empty():
    assert memory_session_id(SimpleNamespace(session_key=""), "fb:1") == "fb__1"
    assert memory_session_id(SimpleNamespace(), "fb:2") == "fb__2"
    assert memory_session_id(None, "fb:3") == "fb__3"
