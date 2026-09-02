"""功能开关与用户数上限的测试。

覆盖五个卡点各自的「未授权时关闭、已授权时放行、授权系统未初始化时不
误伤」三种情形。
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta

import pytest

from mclaw.license import fingerprint as fp_mod
from mclaw.license.manager import LicenseManager, set_manager
from mclaw.license.verifier import PREFIX

_FP = "AAAA-BBBB-CCCC-DDDD-EEEE"


@pytest.fixture(autouse=True)
def isolated_license(monkeypatch):
    """每个用例前后都把全局单例清干净，避免串味。"""
    fp_mod.reset_cache()
    monkeypatch.setattr("mclaw.license.manager.collect_fingerprint", lambda **_: _FP)
    set_manager(None)
    yield
    set_manager(None)
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


def make_code(key, *, features=("plugins", "skills"), users: int = 10) -> str:
    payload = {
        "v": 1,
        "sn": "MC-2026-0001",
        "cust": "测试客户",
        "fp": _FP,
        "iss": (date.today() - timedelta(days=1)).isoformat(),
        "exp": (date.today() + timedelta(days=180)).isoformat(),
        "tier": "",
        "users": users,
        "feat": list(features),
    }
    body = _b64url(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    )
    return f"{PREFIX}.{body}.{_b64url(key.sign(f'{PREFIX}.{body}'.encode('ascii')))}"


def activate(tmp_path, key, **kwargs) -> LicenseManager:
    manager = LicenseManager(tmp_path)
    manager.load()
    manager.activate(make_code(key, **kwargs))
    set_manager(manager)
    return manager


def unlicensed(tmp_path) -> LicenseManager:
    manager = LicenseManager(tmp_path)
    manager.load()
    set_manager(manager)
    return manager


# ── feature_enabled 语义 ──────────────────────────────────────────────


def test_uninitialised_does_not_break_cli():
    """CLI 子命令、单测不初始化授权系统，此时不应被误伤。"""
    from mclaw.license.manager import feature_enabled

    set_manager(None)
    assert feature_enabled("plugins", default=True)
    assert feature_enabled("skills", default=True)
    assert feature_enabled("mcp", default=True)
    assert feature_enabled("im_channels", default=True)


def test_unlicensed_disables_everything(tmp_path):
    from mclaw.license.manager import feature_enabled

    unlicensed(tmp_path)
    for name in ("plugins", "skills", "mcp", "im_channels", "knowledge_base"):
        assert not feature_enabled(name, default=True), name


def test_licensed_enables_only_listed_features(tmp_path, signing_key):
    from mclaw.license.manager import feature_enabled

    activate(tmp_path, signing_key, features=("plugins", "knowledge_base"))
    assert feature_enabled("plugins")
    assert feature_enabled("knowledge_base")
    assert not feature_enabled("skills")
    assert not feature_enabled("mcp")
    assert not feature_enabled("im_channels")


# ── 技能开关 ──────────────────────────────────────────────────────────


def _registry_with_one_skill():
    from mclaw.skills.registry import SkillEntry, SkillRegistry

    registry = SkillRegistry()
    entry = SkillEntry(
        skill_id="demo",
        name="demo",
        description="示例技能",
        keywords=["demo"],
    )
    registry._skills["demo"] = entry
    return registry


def test_skills_hidden_from_llm_when_unlicensed(tmp_path):
    unlicensed(tmp_path)
    registry = _registry_with_one_skill()
    assert registry.get_tool_schemas() == []
    assert registry.find_relevant("demo") == []


def test_skills_visible_when_licensed(tmp_path, signing_key):
    activate(tmp_path, signing_key, features=("skills",))
    registry = _registry_with_one_skill()
    assert len(registry.get_tool_schemas()) == 1
    assert len(registry.find_relevant("demo")) == 1


def test_skills_visible_when_license_uninitialised():
    set_manager(None)
    registry = _registry_with_one_skill()
    assert len(registry.get_tool_schemas()) == 1


# ── IM 通道开关 ───────────────────────────────────────────────────────


def test_im_adapter_not_created_when_unlicensed(tmp_path):
    from mclaw.main import _create_bot_adapter

    unlicensed(tmp_path)
    adapter = _create_bot_adapter(
        "telegram",
        {"token": "x"},
        channel_name="test",
        bot_id="b1",
        agent_profile_id="a1",
    )
    assert adapter is None


# ── MCP 开关 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_connect_refused_when_unlicensed(tmp_path):
    from mclaw.tools.mcp import MCPClient

    unlicensed(tmp_path)
    result = await MCPClient()._connect_runtime("any-server")
    assert not result.success
    assert "未授权" in result.error


# ── 用户数上限 ────────────────────────────────────────────────────────


def _fresh_config(tmp_path):
    from mclaw.api.auth import WebAccessConfig

    return WebAccessConfig(tmp_path)


def test_first_admin_always_creatable_when_unlicensed(tmp_path):
    """装完即锁的前提下，setup 流程仍须能建出第一个 admin。

    否则客户连登录都做不到，也就永远看不到激活页——死锁。
    """
    unlicensed(tmp_path / "lic")
    config = _fresh_config(tmp_path)
    config.add_user("admin", "Str0ng!Passw0rd", role="admin")
    assert config.has_password_set


def test_second_user_blocked_when_unlicensed(tmp_path):
    unlicensed(tmp_path / "lic")
    config = _fresh_config(tmp_path)
    config.add_user("admin", "Str0ng!Passw0rd", role="admin")
    with pytest.raises(ValueError, match="授权最多"):
        config.add_user("bob", "An0ther!Passw0rd")


def test_user_limit_enforced_at_boundary(tmp_path, signing_key):
    activate(tmp_path / "lic", signing_key, users=3)
    config = _fresh_config(tmp_path)
    config.add_user("admin", "Str0ng!Passw0rd", role="admin")
    config.add_user("bob", "Str0ng!Passw0rd")
    config.add_user("carol", "Str0ng!Passw0rd")
    with pytest.raises(ValueError, match="授权最多 3 个用户"):
        config.add_user("dave", "Str0ng!Passw0rd")


def test_zero_users_means_unlimited(tmp_path, signing_key):
    activate(tmp_path / "lic", signing_key, users=0)
    config = _fresh_config(tmp_path)
    for i in range(12):
        config.add_user(f"user{i}", "Str0ng!Passw0rd")
    assert len(config.list_users()) == 12


def test_user_limit_not_enforced_when_uninitialised(tmp_path):
    """未初始化授权系统时不限制——否则单测和 CLI 全挂。"""
    set_manager(None)
    config = _fresh_config(tmp_path)
    for i in range(5):
        config.add_user(f"user{i}", "Str0ng!Passw0rd")
    assert len(config.list_users()) == 5


def test_license_failure_does_not_block_user_management(tmp_path, monkeypatch):
    """授权子系统异常绝不能把用户管理一起拖死。"""
    import mclaw.api.auth as auth_mod

    def boom():
        raise RuntimeError("license subsystem exploded")

    monkeypatch.setattr("mclaw.license.manager.get_manager", boom)
    config = _fresh_config(tmp_path)
    config.add_user("admin", "Str0ng!Passw0rd", role="admin")
    assert config.has_password_set
    assert auth_mod._license_user_limit() == 0
