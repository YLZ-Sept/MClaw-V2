"""授权状态机、持久化与时钟回拨防护的单元测试。"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from mclaw.license import fingerprint as fp_mod
from mclaw.license.manager import (
    GRACE_PERIOD_DAYS,
    WARN_BEFORE_DAYS,
    LicenseManager,
    LicenseState,
)
from mclaw.license.verifier import PREFIX, LicenseVerifyError

_FP = "AAAA-BBBB-CCCC-DDDD-EEEE"


@pytest.fixture(autouse=True)
def stub_fingerprint(monkeypatch):
    """固定本机指纹，避免测试触碰真实硬件（且单次采集约 1.25 秒）。"""
    fp_mod.reset_cache()
    monkeypatch.setattr(fp_mod, "collect_fingerprint", lambda **_: _FP)
    monkeypatch.setattr("mclaw.license.manager.collect_fingerprint", lambda **_: _FP)
    yield
    fp_mod.reset_cache()


@pytest.fixture
def signing_key(monkeypatch):
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    raw_pub = key.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)
    monkeypatch.setattr("mclaw.license.verifier.public_key_bytes", lambda: (raw_pub,))
    return key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_code(key, *, days_valid: int = 180, fp: str = _FP, **overrides) -> str:
    expires = date.today() + timedelta(days=days_valid)
    # 签发日必须早于到期日。测试过期场景时 days_valid 为负，此时把签发日
    # 再往前推，否则 verifier 会先以「到期日早于签发日」拒绝，测不到过期分支。
    issued = min(date.today() - timedelta(days=1), expires - timedelta(days=1))
    payload = {
        "v": 1,
        "sn": "MC-2026-0001",
        "cust": "测试客户",
        "fp": fp,
        "iss": issued.isoformat(),
        "exp": expires.isoformat(),
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


@pytest.fixture
def manager(tmp_path):
    return LicenseManager(tmp_path)


# ── 未激活 ────────────────────────────────────────────────────────────


def test_fresh_install_is_missing(manager):
    status = manager.load()
    assert status.state is LicenseState.MISSING
    assert not status.allows_access


def test_unactivated_blocks_all_features(manager):
    manager.load()
    for feature in ("plugins", "skills", "mcp", "knowledge_base", "im_channels"):
        assert not manager.has_feature(feature)


def test_unactivated_user_limit_is_one(manager):
    """未激活时只允许一个用户——就是完成 setup 的那个 admin。"""
    manager.load()
    assert manager.max_users() == 1


# ── 激活 ──────────────────────────────────────────────────────────────


def test_activate_and_reload(manager, signing_key):
    status = manager.activate(make_code(signing_key))
    assert status.state is LicenseState.ACTIVE
    assert status.payload.customer == "测试客户"

    # 新进程重启：从磁盘重新加载并完整验签
    reloaded = LicenseManager(manager._path.parent).load()
    assert reloaded.state is LicenseState.ACTIVE
    assert reloaded.payload.serial == "MC-2026-0001"


def test_activate_enables_licensed_features_only(manager, signing_key):
    manager.activate(make_code(signing_key, feat=["plugins", "knowledge_base"]))
    assert manager.has_feature("plugins")
    assert manager.has_feature("knowledge_base")
    assert not manager.has_feature("mcp")
    assert not manager.has_feature("im_channels")


def test_activate_sets_user_limit(manager, signing_key):
    manager.activate(make_code(signing_key, users=25))
    assert manager.max_users() == 25


def test_activate_rejects_foreign_fingerprint(manager, signing_key):
    with pytest.raises(LicenseVerifyError, match="不匹配"):
        manager.activate(make_code(signing_key, fp="1111-2222-3333-4444-5555"))


def test_activate_rejects_expired_code(manager, signing_key):
    with pytest.raises(LicenseVerifyError, match="过期"):
        manager.activate(make_code(signing_key, days_valid=-(GRACE_PERIOD_DAYS + 5)))


def test_activate_rejects_tampered_code(manager, signing_key):
    code = make_code(signing_key)
    head, body, sig = code.split(".")
    bad = f"{head}.{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{sig}"
    with pytest.raises(LicenseVerifyError):
        manager.activate(bad)


def test_bad_code_does_not_overwrite_good_one(manager, signing_key):
    """校验失败时不写盘——否则一个坏码会顶掉客户正在用的好码。"""
    manager.activate(make_code(signing_key, users=25))

    with pytest.raises(LicenseVerifyError):
        manager.activate("MC1.garbage.garbage")

    reloaded = LicenseManager(manager._path.parent).load()
    assert reloaded.state is LicenseState.ACTIVE
    assert reloaded.payload.max_users == 25


def test_activate_refuses_when_too_few_hardware_ids(manager, signing_key, monkeypatch):
    """硬件特征不足时当场拒绝，而不是签一个必然失效的授权。"""
    blank = "-".join([fp_mod.MISSING_SEGMENT] * 5)
    monkeypatch.setattr("mclaw.license.manager.collect_fingerprint", lambda **_: blank)
    with pytest.raises(LicenseVerifyError, match="硬件特征"):
        manager.activate(make_code(signing_key, fp=blank))


# ── 到期 / 宽限期 ─────────────────────────────────────────────────────


def test_within_grace_period_still_allows_access(manager, signing_key):
    manager.activate(make_code(signing_key, days_valid=180))
    # 直接构造一个已过期 3 天的码，绕过 activate 的过期拒绝
    manager._status = manager._evaluate(
        make_code(signing_key, days_valid=-3), clock_ok=True
    )
    status = manager.status
    assert status.state is LicenseState.GRACE
    assert status.allows_access
    assert status.should_warn
    assert manager.has_feature("plugins")


def test_beyond_grace_period_blocks(manager, signing_key):
    manager._status = manager._evaluate(
        make_code(signing_key, days_valid=-(GRACE_PERIOD_DAYS + 1)), clock_ok=True
    )
    status = manager.status
    assert status.state is LicenseState.EXPIRED
    assert not status.allows_access
    assert not manager.has_feature("plugins")


def test_grace_boundary_last_day_still_allowed(manager, signing_key):
    manager._status = manager._evaluate(
        make_code(signing_key, days_valid=-GRACE_PERIOD_DAYS), clock_ok=True
    )
    assert manager.status.state is LicenseState.GRACE


def test_warning_appears_before_expiry(manager, signing_key):
    manager.activate(make_code(signing_key, days_valid=WARN_BEFORE_DAYS - 1))
    status = manager.status
    assert status.state is LicenseState.ACTIVE
    assert status.should_warn


def test_no_warning_when_far_from_expiry(manager, signing_key):
    manager.activate(make_code(signing_key, days_valid=WARN_BEFORE_DAYS + 30))
    assert not manager.status.should_warn


def test_warning_message_tiered_by_remaining_days(signing_key, tmp_path):
    """临期文案按剩余天数分档，横幅正文不再自相矛盾。

    之前临近到期时 ``message`` 仍硬编码"授权有效"，前端黄条上会同时
    出现"快到期"和"一切正常"。此处锁死三档措辞；每档用独立 manager，
    避免同一实例上重复激活引入状态耦合。
    """

    def status_for(days_valid):
        mgr = LicenseManager(tmp_path / f"d{days_valid}")
        mgr.activate(make_code(signing_key, days_valid=days_valid))
        return mgr.status

    # 远离到期：中性文案
    far = status_for(WARN_BEFORE_DAYS + 30)
    assert far.state is LicenseState.ACTIVE
    assert far.message == "授权有效"

    # 临期：写明到期日与剩余天数，前端黄条据此渲染
    near = status_for(WARN_BEFORE_DAYS - 1)
    assert near.state is LicenseState.ACTIVE
    assert near.should_warn
    assert near.message.startswith("授权将于")
    assert "到期" in near.message
    assert str(near.days_remaining) in near.message

    # 当天到期：最紧迫的措辞
    today = status_for(0)
    assert today.state is LicenseState.ACTIVE
    assert today.days_remaining == 0
    assert "今天" in today.message


# ── 时钟回拨 ──────────────────────────────────────────────────────────


def test_clock_rollback_detected(manager, signing_key, tmp_path):
    manager.activate(make_code(signing_key))

    # 伪造一个「未来」的水位线，模拟客户把系统时间往回调
    stored = json.loads((tmp_path / "license.json").read_text(encoding="utf-8"))
    stored["last_seen_utc"] = (
        datetime.now(timezone.utc) + timedelta(days=10)
    ).isoformat()
    (tmp_path / "license.json").write_text(
        json.dumps(stored, ensure_ascii=False), encoding="utf-8"
    )

    status = LicenseManager(tmp_path).load()
    assert status.state is LicenseState.INVALID
    assert not status.allows_access
    assert "时间" in status.message


def test_small_clock_drift_tolerated(manager, signing_key, tmp_path):
    """NTP 校正、时区调整不应误伤正版客户。"""
    manager.activate(make_code(signing_key))

    stored = json.loads((tmp_path / "license.json").read_text(encoding="utf-8"))
    stored["last_seen_utc"] = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat()
    (tmp_path / "license.json").write_text(
        json.dumps(stored, ensure_ascii=False), encoding="utf-8"
    )

    assert LicenseManager(tmp_path).load().state is LicenseState.ACTIVE


def test_corrupt_heartbeat_does_not_lock_out(manager, signing_key, tmp_path):
    manager.activate(make_code(signing_key))
    stored = json.loads((tmp_path / "license.json").read_text(encoding="utf-8"))
    stored["last_seen_utc"] = "not-a-timestamp"
    (tmp_path / "license.json").write_text(
        json.dumps(stored, ensure_ascii=False), encoding="utf-8"
    )
    assert LicenseManager(tmp_path).load().state is LicenseState.ACTIVE


def test_heartbeat_is_throttled(manager, signing_key, tmp_path):
    """心跳在请求路径上调用，必须廉价——不能每次都写盘。"""
    manager.activate(make_code(signing_key))
    path = tmp_path / "license.json"

    manager.touch_heartbeat()
    first = path.read_text(encoding="utf-8")
    for _ in range(100):
        manager.touch_heartbeat()
    assert path.read_text(encoding="utf-8") == first


# ── 持久化健壮性 ──────────────────────────────────────────────────────


def test_corrupt_license_file_treated_as_missing(tmp_path):
    (tmp_path / "license.json").write_text("{ not json", encoding="utf-8")
    assert LicenseManager(tmp_path).load().state is LicenseState.MISSING


def test_empty_code_field_treated_as_missing(tmp_path):
    (tmp_path / "license.json").write_text('{"code": ""}', encoding="utf-8")
    assert LicenseManager(tmp_path).load().state is LicenseState.MISSING


def test_status_dict_never_leaks_code(manager, signing_key):
    """状态接口对所有已登录用户开放，不能回传授权码原文。"""
    manager.activate(make_code(signing_key))
    payload = manager.status.to_dict()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "MC1." not in serialized
    assert "code" not in payload


def test_status_dict_shape(manager, signing_key):
    manager.activate(make_code(signing_key, users=25))
    data = manager.status.to_dict()
    assert data["state"] == "active"
    assert data["allows_access"] is True
    assert data["max_users"] == 25
    assert data["serial"] == "MC-2026-0001"
    assert sorted(data["features"]) == ["plugins", "skills"]


# ── 全局访问点 ────────────────────────────────────────────────────────


def test_feature_enabled_default_when_uninitialised():
    """CLI / 单测等未初始化授权系统的场景不应被误伤。"""
    from mclaw.license.manager import feature_enabled, set_manager

    set_manager(None)
    assert feature_enabled("plugins", default=True)
    assert not feature_enabled("plugins", default=False)


def test_feature_enabled_reads_global_manager(manager, signing_key):
    from mclaw.license.manager import feature_enabled, set_manager

    manager.activate(make_code(signing_key, feat=["plugins"]))
    set_manager(manager)
    try:
        assert feature_enabled("plugins")
        assert not feature_enabled("mcp")
    finally:
        set_manager(None)
