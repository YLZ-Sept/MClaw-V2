"""license gate 中间件的行为测试。

用最小 FastAPI 应用复现真实的中间件顺序（CORS → setup → auth → license），
验证白名单、402 信封、以及「激活接口必须已登录且为 admin」这条边界。
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from mclaw.api.middleware_license_gate import create_license_gate_middleware
from mclaw.license import fingerprint as fp_mod
from mclaw.license.manager import LicenseManager
from mclaw.license.verifier import PREFIX

_FP = "AAAA-BBBB-CCCC-DDDD-EEEE"


@pytest.fixture(autouse=True)
def stub_fingerprint(monkeypatch):
    fp_mod.reset_cache()
    monkeypatch.setattr("mclaw.license.manager.collect_fingerprint", lambda **_: _FP)
    monkeypatch.setattr("mclaw.api.routes.license.collect_fingerprint", lambda **_: _FP)
    monkeypatch.setattr(
        "mclaw.api.routes.license.fingerprint_detail",
        lambda **_: {"board": True, "bios": True, "disk": True, "mac": True, "guid": True},
    )
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
    return f"{PREFIX}.{body}.{_b64url(key.sign(f'{PREFIX}.{body}'.encode('ascii')))}"


class _FakeWebAccess:
    """最小 WebAccessConfig 替身，只提供 is_admin。"""

    def __init__(self, admins=("admin",)):
        self._admins = set(admins)

    def is_admin(self, username: str) -> bool:
        return username in self._admins


def build_app(manager: LicenseManager, *, user: str | None = "admin") -> FastAPI:
    """搭一个复刻真实中间件顺序的最小应用。

    ``user`` 模拟 auth 中间件的产出：``None`` 表示未登录。
    """
    from mclaw.api.routes import license as license_routes

    app = FastAPI()
    app.state.license_manager = manager
    app.state.web_access_config = _FakeWebAccess()
    app.include_router(license_routes.router)

    @app.get("/api/chat")
    async def business_endpoint():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    # license gate 最先注册 → 入站方向排在 auth 之后
    app.middleware("http")(create_license_gate_middleware(manager))

    # 伪 auth 中间件：后注册 → 先执行，与生产一致
    @app.middleware("http")
    async def fake_auth(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/auth/") or path in ("/api/health", "/"):
            return await call_next(request)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "unauthorised"})
        request.state.user_id = user
        return await call_next(request)

    return app


@pytest.fixture
def manager(tmp_path):
    m = LicenseManager(tmp_path)
    m.load()
    return m


# ── 未激活时的拦截 ────────────────────────────────────────────────────


def test_business_endpoint_blocked_when_unlicensed(manager):
    with TestClient(build_app(manager)) as client:
        res = client.get("/api/chat")
    assert res.status_code == 402
    body = res.json()
    assert body["error"] == "license_required"
    assert body["state"] == "missing"
    assert body["license_url"] == "/web/#/license"


def test_health_probe_always_allowed(manager):
    with TestClient(build_app(manager)) as client:
        assert client.get("/api/health").status_code == 200


def test_license_endpoints_reachable_when_unlicensed(manager):
    """未激活时这三个接口必须可达——它们是唯一的自救出口。"""
    with TestClient(build_app(manager)) as client:
        assert client.get("/api/license/status").status_code == 200
        assert client.get("/api/license/fingerprint").status_code == 200


def test_options_preflight_passes(manager):
    with TestClient(build_app(manager)) as client:
        res = client.options("/api/chat")
    assert res.status_code != 402


# ── 授权有效后放行 ────────────────────────────────────────────────────


def test_business_endpoint_allowed_after_activation(manager, signing_key):
    manager.activate(make_code(signing_key))
    with TestClient(build_app(manager)) as client:
        res = client.get("/api/chat")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_expired_license_blocks_with_expired_code(manager, signing_key):
    from mclaw.license.manager import GRACE_PERIOD_DAYS

    manager._status = manager._evaluate(
        make_code(signing_key, days_valid=-(GRACE_PERIOD_DAYS + 5)), clock_ok=True
    )
    with TestClient(build_app(manager)) as client:
        res = client.get("/api/chat")
    assert res.status_code == 402
    assert res.json()["error"] == "license_expired"


def test_grace_period_still_serves_traffic(manager, signing_key):
    manager._status = manager._evaluate(
        make_code(signing_key, days_valid=-2), clock_ok=True
    )
    with TestClient(build_app(manager)) as client:
        assert client.get("/api/chat").status_code == 200


def test_mismatch_reports_distinct_error_code(manager, signing_key):
    manager._status = manager._evaluate(
        make_code(signing_key, fp="1111-2222-3333-4444-5555"), clock_ok=True
    )
    with TestClient(build_app(manager)) as client:
        res = client.get("/api/chat")
    assert res.status_code == 402
    assert res.json()["error"] == "license_mismatch"


# ── 激活接口的权限边界 ────────────────────────────────────────────────


def test_activate_requires_login(manager, signing_key):
    """未登录时连激活接口都进不去——否则局域网任何人都能改授权。"""
    with TestClient(build_app(manager, user=None)) as client:
        res = client.post("/api/license/activate", json={"code": make_code(signing_key)})
    assert res.status_code == 401


def test_activate_requires_admin(manager, signing_key):
    with TestClient(build_app(manager, user="bob")) as client:
        res = client.post("/api/license/activate", json={"code": make_code(signing_key)})
    assert res.status_code == 403


def test_activate_succeeds_for_admin(manager, signing_key):
    with TestClient(build_app(manager)) as client:
        res = client.post("/api/license/activate", json={"code": make_code(signing_key)})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["status"] == "ok"
        assert body["restart_required"] is True
        assert body["license"]["state"] == "active"

        # 激活后同一进程内立即放行，无需重启（中间件按引用捕获 manager）
        assert client.get("/api/chat").status_code == 200


def test_activate_rejects_garbage(manager):
    with TestClient(build_app(manager)) as client:
        res = client.post("/api/license/activate", json={"code": "MC1.bad.bad"})
    assert res.status_code == 400


def test_activate_rejects_empty_code(manager):
    with TestClient(build_app(manager)) as client:
        res = client.post("/api/license/activate", json={"code": "   "})
    assert res.status_code == 400


def test_activate_rejects_foreign_machine_code(manager, signing_key):
    with TestClient(build_app(manager)) as client:
        res = client.post(
            "/api/license/activate",
            json={"code": make_code(signing_key, fp="9999-8888-7777-6666-5555")},
        )
    assert res.status_code == 400
    assert "不匹配" in res.json()["detail"]


# ── 状态接口不泄露授权码 ──────────────────────────────────────────────


def test_status_does_not_leak_code(manager, signing_key):
    manager.activate(make_code(signing_key))
    with TestClient(build_app(manager, user="bob")) as client:
        res = client.get("/api/license/status")
    assert res.status_code == 200
    assert "MC1." not in res.text


def test_fingerprint_endpoint_shape(manager):
    with TestClient(build_app(manager, user="bob")) as client:
        body = client.get("/api/license/fingerprint").json()
    assert body["fingerprint"] == _FP
    assert body["usable_segments"] == 5
    assert body["sufficient"] is True
    assert len(body["components"]) == 5
