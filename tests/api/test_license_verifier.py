"""授权码验签与硬件指纹的单元测试。

不依赖真实硬件——指纹采集被 monkeypatch 掉，只测纯逻辑。
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta

import pytest

from mclaw.license import fingerprint as fp_mod
from mclaw.license.fingerprint import (
    MISSING_SEGMENT,
    _hash_segment,
    _is_placeholder,
    fingerprint_matches,
    usable_segment_count,
)
from mclaw.license.verifier import (
    PREFIX,
    LicenseVerifyError,
    verify_license_code,
)

# ── 测试用密钥 ────────────────────────────────────────────────────────


@pytest.fixture
def signing_key(monkeypatch):
    """生成一次性密钥对，并把公钥注入验签模块。"""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    raw_pub = key.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    monkeypatch.setattr(
        "mclaw.license.verifier.public_key_bytes", lambda: (raw_pub,)
    )
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_code(key, **overrides) -> str:
    """用测试密钥签一个授权码。"""
    payload = {
        "v": 1,
        "sn": "MC-2026-0001",
        "cust": "测试客户",
        "fp": "AAAA-BBBB-CCCC-DDDD-EEEE",
        "iss": date.today().isoformat(),
        "exp": (date.today() + timedelta(days=180)).isoformat(),
        "tier": "",
        "users": 10,
        "feat": ["plugins", "skills"],
    }
    payload.update(overrides)
    body = _b64url(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    sig = key.sign(f"{PREFIX}.{body}".encode("ascii"))
    return f"{PREFIX}.{body}.{_b64url(sig)}"


# ── 验签 ──────────────────────────────────────────────────────────────


def test_valid_code_round_trip(signing_key):
    payload = verify_license_code(make_code(signing_key))
    assert payload.customer == "测试客户"
    assert payload.max_users == 10
    assert payload.features == frozenset({"plugins", "skills"})
    assert payload.has_feature("plugins")
    assert not payload.has_feature("mcp")


def test_whitespace_and_newlines_tolerated(signing_key):
    """客户从邮件复制常带换行。"""
    code = make_code(signing_key)
    mangled = code[:40] + "\n  " + code[40:80] + "\r\n" + code[80:]
    assert verify_license_code(mangled).serial == "MC-2026-0001"


def test_tampered_payload_rejected(signing_key):
    code = make_code(signing_key)
    head, body, sig = code.split(".")
    # 翻转 payload 中的一个字符
    flipped = body[:-1] + ("A" if body[-1] != "A" else "B")
    with pytest.raises(LicenseVerifyError, match="签名校验失败"):
        verify_license_code(f"{head}.{flipped}.{sig}")


def test_foreign_signature_rejected(signing_key):
    """他人用自己的私钥签的码必须拒绝。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    attacker = Ed25519PrivateKey.generate()
    with pytest.raises(LicenseVerifyError, match="签名校验失败"):
        verify_license_code(make_code(attacker))


@pytest.mark.parametrize(
    "bad",
    ["", "not-a-code", "MC1.only-two-parts", "MC9.aaa.bbb", "MC1..", "a.b.c.d"],
)
def test_malformed_codes_rejected(signing_key, bad):
    with pytest.raises(LicenseVerifyError):
        verify_license_code(bad)


def test_expiry_before_issue_rejected(signing_key):
    code = make_code(
        signing_key, iss="2026-09-01", exp="2026-08-01"
    )
    with pytest.raises(LicenseVerifyError, match="早于签发日"):
        verify_license_code(code)


def test_negative_user_count_rejected(signing_key):
    with pytest.raises(LicenseVerifyError, match="用户数字段无效"):
        verify_license_code(make_code(signing_key, users=-5))


def test_bool_user_count_rejected(signing_key):
    """bool 是 int 的子类，必须显式排除。"""
    with pytest.raises(LicenseVerifyError, match="用户数字段无效"):
        verify_license_code(make_code(signing_key, users=True))


def test_unknown_feature_ignored_not_fatal(signing_key):
    """新版签发的码在老客户端上仍应可用。"""
    payload = verify_license_code(
        make_code(signing_key, feat=["plugins", "future_module_x"])
    )
    assert payload.has_feature("plugins")
    assert payload.has_feature("future_module_x")


def test_unsupported_protocol_version_rejected(signing_key):
    with pytest.raises(LicenseVerifyError, match="协议版本不支持"):
        verify_license_code(make_code(signing_key, v=2))


def test_zero_users_means_unlimited(signing_key):
    assert verify_license_code(make_code(signing_key, users=0)).unlimited_users


# ── 指纹匹配 ──────────────────────────────────────────────────────────

_CURRENT = "AAAA-BBBB-CCCC-DDDD-EEEE"


def test_identical_fingerprint_matches():
    assert fingerprint_matches(_CURRENT, _CURRENT) == (True, 5)


def test_one_component_swapped_still_matches():
    """换一块硬盘不应锁死正版客户——这是独立哈希方案的核心价值。"""
    swapped = "AAAA-BBBB-9999-DDDD-EEEE"
    ok, matched = fingerprint_matches(swapped, _CURRENT)
    assert ok and matched == 4


def test_two_components_swapped_still_matches():
    swapped = "AAAA-BBBB-9999-8888-EEEE"
    ok, matched = fingerprint_matches(swapped, _CURRENT)
    assert ok and matched == 3


def test_three_components_swapped_rejected():
    """整机更换必然失配。"""
    swapped = "AAAA-BBBB-9999-8888-7777"
    ok, matched = fingerprint_matches(swapped, _CURRENT)
    assert not ok and matched == 2


def test_completely_different_machine_rejected():
    ok, matched = fingerprint_matches("1111-2222-3333-4444-5555", _CURRENT)
    assert not ok and matched == 0


def test_missing_segments_never_count_as_match():
    """两台机器同样缺失某部件，不构成「相同」的证据。

    若缺失段参与匹配，两台都取不到盘序列号和 BIOS 的机器会凭空多出
    2 段匹配，配合任意 1 段真实匹配即可越过阈值。
    """
    licensed = f"AAAA-{MISSING_SEGMENT}-{MISSING_SEGMENT}-{MISSING_SEGMENT}-{MISSING_SEGMENT}"
    current = f"AAAA-{MISSING_SEGMENT}-{MISSING_SEGMENT}-{MISSING_SEGMENT}-{MISSING_SEGMENT}"
    ok, matched = fingerprint_matches(licensed, current)
    assert not ok and matched == 1


def test_all_missing_never_matches():
    blank = "-".join([MISSING_SEGMENT] * 5)
    assert fingerprint_matches(blank, blank) == (False, 0)


def test_wrong_segment_count_rejected():
    assert fingerprint_matches("AAAA-BBBB", _CURRENT) == (False, 0)
    assert fingerprint_matches(_CURRENT, "AAAA-BBBB") == (False, 0)


def test_case_insensitive_matching():
    assert fingerprint_matches(_CURRENT.lower(), _CURRENT)[0]


def test_usable_segment_count():
    assert usable_segment_count(_CURRENT) == 5
    assert usable_segment_count(f"AAAA-{MISSING_SEGMENT}-CCCC-DDDD-EEEE") == 4
    assert usable_segment_count("-".join([MISSING_SEGMENT] * 5)) == 0


# ── 段位哈希 ──────────────────────────────────────────────────────────


def test_segment_hash_is_stable():
    assert _hash_segment("board", "ABC123") == _hash_segment("board", "ABC123")


def test_segment_hash_normalizes_case_and_space():
    assert _hash_segment("board", " abc123 ") == _hash_segment("board", "ABC123")


def test_same_value_different_component_yields_different_segment():
    """部件类型混入摘要，避免两个部件取到同值时段位相同。"""
    assert _hash_segment("board", "SAME") != _hash_segment("bios", "SAME")


def test_segment_hash_avalanche():
    """独立哈希：单个部件变化只影响自己那一段。"""
    a = _hash_segment("disk", "SERIAL-001")
    b = _hash_segment("disk", "SERIAL-002")
    assert a != b


# ── 厂商占位串检测 ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "",
        "  ",
        "To Be Filled By O.E.M.",
        "Default string",
        "System Serial Number",
        "00000000",
        "0000-0000-0000",
        "FFFFFFFF",
        "00000000-0000-0000-0000-000000000000",
        "None",
        "unknown",
    ],
)
def test_placeholder_values_detected(value):
    assert _is_placeholder(value)


@pytest.mark.parametrize(
    "value", ["EGW58M103XN", "T8NRKD01C73134C", "A0:AD:9F:9B:D6:9E", "9C6CCA70"]
)
def test_real_values_not_flagged_as_placeholder(value):
    assert not _is_placeholder(value)


def test_placeholder_becomes_missing_segment(monkeypatch):
    """厂商占位串必须降级为缺失段，否则同型号机器会互相匹配。"""
    monkeypatch.setattr(
        fp_mod,
        "_collect_raw_windows",
        lambda: [
            "REAL-BOARD",
            "To Be Filled By O.E.M.",
            "REAL-DISK",
            "",
            "REAL-GUID",
        ],
    )
    fp_mod.reset_cache()
    fingerprint, detail = fp_mod.compute_fingerprint()
    segments = fingerprint.split("-")
    assert segments[1] == MISSING_SEGMENT
    assert segments[3] == MISSING_SEGMENT
    assert detail == {
        "board": True,
        "bios": False,
        "disk": True,
        "mac": False,
        "guid": True,
    }
    fp_mod.reset_cache()


def test_collect_is_cached(monkeypatch):
    """指纹采集耗时约 1.25 秒，绝不能每请求执行。"""
    calls = []

    def fake_collect():
        calls.append(1)
        return ["A", "B", "C", "D", "E"]

    monkeypatch.setattr(fp_mod, "_collect_raw_windows", fake_collect)
    fp_mod.reset_cache()
    first = fp_mod.collect_fingerprint()
    for _ in range(50):
        assert fp_mod.collect_fingerprint() == first
    assert len(calls) == 1
    fp_mod.reset_cache()
