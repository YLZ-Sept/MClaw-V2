"""认证流程端到端集成测试。

覆盖多用户化 + 安全加固（2026-08 移除 loopback 豁免）之后的认证契约：

1. 受保护接口一律需要凭证——**本机直连不再免认证**
2. 豁免仅限白名单路径，见 ``mclaw.api.auth.AUTH_EXEMPT_PATHS`` /
   ``AUTH_EXEMPT_PREFIXES``（/、/api/health、/api/build-info、/api/auth/*、
   /api/logs/frontend、/web/、/ws/、/docs、/openapi.json、/redoc、/user-docs）
3. ``TRUST_PROXY`` 只影响限速用的客户端 IP 判定，**不再影响是否需要认证**
4. WebSocket 走独立鉴权：``/ws/`` 前缀豁免 HTTP 中间件，连接级鉴权见
   ``routes.websocket._authenticate_ws``
5. /web/ 路径始终免认证
6. /api/health 免认证
6b. /api/build-info 免认证（陈旧包横幅须在登录前也能探测）
7. /api/config 需要认证
8. 记忆管理 API 需要认证（曾有 401 bug）
9. token 刷新流程
10. 插件 UI 静态资源免认证，但插件业务 API 仍需认证（含 POST）
"""

from __future__ import annotations

import httpx
import pytest

from mclaw.api.server import create_app

TEST_PASSWORD = "integration-test-pw-42"


@pytest.fixture
def app(monkeypatch, tmp_path):
    from mclaw.config import settings

    monkeypatch.setattr(settings, "project_root", tmp_path)
    # 实现在 api/auth.py 的 PASSWORD_ENV_VAR 里读的是 OPENAKITA_WEB_PASSWORD
    # （该常量未随品牌改名），清掉以免宿主环境干扰。
    monkeypatch.delenv("OPENAKITA_WEB_PASSWORD", raising=False)
    monkeypatch.delenv("TRUST_PROXY", raising=False)

    app = create_app()
    # 多用户化之后，空配置里不会自动建 admin（只有 v1 迁移或设了
    # OPENAKITA_WEB_PASSWORD 时才会），所以测试要显式创建管理员。
    app.state.web_access_config.add_user("admin", TEST_PASSWORD, role="admin")
    return app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c


@pytest.fixture
def access_token(app) -> str:
    config = app.state.web_access_config
    return config.create_access_token("admin")


@pytest.fixture
def refresh_token(app) -> str:
    config = app.state.web_access_config
    return config.create_refresh_token("admin")


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. 本机直连**不再**免认证（loopback 豁免已于 2026-08 移除）
# ---------------------------------------------------------------------------


class TestLocalDirectRequiresAuth:
    async def test_local_request_requires_auth(self, client):
        resp = await client.get("/api/config/workspace-info")
        assert resp.status_code == 401

    async def test_local_request_to_protected_endpoint_requires_auth(self, client):
        resp = await client.get("/api/memories")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. 反向代理模式 + X-Forwarded-For → 需要认证
# ---------------------------------------------------------------------------


class TestReverseProxyRequiresAuth:
    async def test_proxy_with_forwarded_for_requires_auth(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/config/workspace-info",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code == 401

    async def test_proxy_with_forwarded_for_and_token_succeeds(
        self, client, access_token, monkeypatch
    ):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/config/workspace-info",
            headers={
                "X-Forwarded-For": "203.0.113.50",
                **auth_header(access_token),
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. 反向代理模式下，本机直连（无 X-Forwarded-For）同样需要认证
# ---------------------------------------------------------------------------


class TestReverseProxyLocalDirect:
    async def test_proxy_mode_local_no_forwarded_for_requires_auth(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get("/api/config/workspace-info")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 4. TRUST_PROXY 不再影响是否需要认证
#    （移除 loopback 豁免后，它只剩"限速用的客户端 IP 判定"这一个作用，
#      见 api/auth.py 的 get_client_ip / is_trusted_local）
# ---------------------------------------------------------------------------


class TestTrustProxyDoesNotGateAuth:
    async def test_protected_endpoint_requires_auth_regardless_of_trust_proxy(
        self, client, access_token, monkeypatch
    ):
        for value in (None, "true"):
            if value is None:
                monkeypatch.delenv("TRUST_PROXY", raising=False)
            else:
                monkeypatch.setenv("TRUST_PROXY", value)

            resp = await client.get(
                "/api/config/workspace-info",
                headers={"X-Forwarded-For": "203.0.113.50"},
            )
            assert resp.status_code == 401, f"TRUST_PROXY={value} 时不应放行"

            resp = await client.get(
                "/api/config/workspace-info",
                headers={
                    "X-Forwarded-For": "203.0.113.50",
                    **auth_header(access_token),
                },
            )
            assert resp.status_code == 200, f"TRUST_PROXY={value} 时带凭证应放行"


# ---------------------------------------------------------------------------
# 5. WebSocket 认证与 HTTP 一致
# ---------------------------------------------------------------------------


class TestWebSocketAuth:
    """WS 认证与 HTTP 完全一致：本机直连**也必须**带 token。

    loopback 豁免在 HTTP 侧已于 2026-08 移除，WS 侧 2026-10 才对齐——此前任何
    能连到本机端口的进程都能凭 ``desktop_user`` 订阅全部事件流，绕过认证。
    """

    async def test_ws_local_direct_without_token_rejected(self, app):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with (
            TestClient(app) as tc,
            pytest.raises(WebSocketDisconnect),
            tc.websocket_connect("/ws/events"),
        ):
            pass

    async def test_ws_local_direct_with_valid_token_connects(self, app, access_token):
        from starlette.testclient import TestClient

        with (
            TestClient(app) as tc,
            tc.websocket_connect(f"/ws/events?token={access_token}") as ws,
        ):
            data = ws.receive_json()
            assert data["event"] == "connected"

    async def test_ws_invalid_token_rejected(self, app):
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with (
            TestClient(app) as tc,
            pytest.raises(WebSocketDisconnect),
            tc.websocket_connect("/ws/events?token=not-a-real-token"),
        ):
            pass

    async def test_ws_proxy_without_token_rejected(self, app, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        from starlette.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect

        with (
            TestClient(app) as tc,
            pytest.raises(WebSocketDisconnect),
            tc.websocket_connect(
                "/ws/events",
                headers={"X-Forwarded-For": "203.0.113.50"},
            ),
        ):
            pass

    async def test_ws_proxy_with_valid_token_connects(self, app, access_token, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        from starlette.testclient import TestClient

        with (
            TestClient(app) as tc,
            tc.websocket_connect(
                f"/ws/events?token={access_token}",
                headers={"X-Forwarded-For": "203.0.113.50"},
            ) as ws,
        ):
            data = ws.receive_json()
            assert data["event"] == "connected"


# ---------------------------------------------------------------------------
# 6. /web/ 路径始终免认证
# ---------------------------------------------------------------------------


class TestWebPathExempt:
    async def test_web_path_no_auth_with_proxy(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/web/",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# 7. /api/health 免认证
# ---------------------------------------------------------------------------


class TestHealthExempt:
    async def test_health_no_auth(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/health",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 6b. /api/build-info 免认证
# ---------------------------------------------------------------------------


class TestBuildInfoExempt:
    async def test_build_info_no_auth(self, client, monkeypatch):
        # The stale-bundle banner probes this before the operator logs in, so
        # it must answer without a credential (route docstring: "intentionally
        # unauthenticated ... without worrying about login state").
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/build-info",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code == 200
        assert resp.json().get("build_id")


# ---------------------------------------------------------------------------
# 8. /api/config 需要认证
# ---------------------------------------------------------------------------


class TestConfigRequiresAuth:
    async def test_config_rejected_without_token(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/config/workspace-info",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code == 401

    async def test_config_allowed_with_token(self, client, access_token, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/config/workspace-info",
            headers={
                "X-Forwarded-For": "203.0.113.50",
                **auth_header(access_token),
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 9. 记忆管理 API 需要认证
# ---------------------------------------------------------------------------


class TestMemoryRequiresAuth:
    async def test_memories_rejected_without_token(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/memories",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code == 401

    async def test_memories_allowed_with_token(self, client, access_token, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/memories",
            headers={
                "X-Forwarded-For": "203.0.113.50",
                **auth_header(access_token),
            },
        )
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# 10. token 刷新流程
# ---------------------------------------------------------------------------


class TestTokenRefresh:
    async def test_refresh_returns_new_access_token(self, client, refresh_token):
        resp = await client.post(
            "/api/auth/refresh",
            cookies={"mclaw_refresh": refresh_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data

    async def test_refresh_with_invalid_token_fails(self, client):
        resp = await client.post(
            "/api/auth/refresh",
            cookies={"mclaw_refresh": "invalid.token.here"},
        )
        assert resp.status_code == 401

    async def test_refreshed_token_can_access_protected_endpoint(
        self, client, refresh_token, monkeypatch
    ):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.post(
            "/api/auth/refresh",
            cookies={"mclaw_refresh": refresh_token},
        )
        assert resp.status_code == 200
        new_token = resp.json()["access_token"]

        resp = await client.get(
            "/api/config/workspace-info",
            headers={
                "X-Forwarded-For": "203.0.113.50",
                **auth_header(new_token),
            },
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 11. Plugin iframe static assets are public; plugin APIs stay protected
# ---------------------------------------------------------------------------


class TestPluginUiRemoteAuth:
    async def test_plugin_ui_document_does_not_require_auth(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/plugins/example/ui/",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code != 401

    async def test_plugin_ui_asset_head_does_not_require_auth(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.head(
            "/api/plugins/example/ui/_assets/app.js",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code != 401

    async def test_plugin_business_api_still_requires_auth(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.get(
            "/api/plugins/example/tasks",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code == 401

    async def test_plugin_ui_post_still_requires_auth(self, client, monkeypatch):
        monkeypatch.setenv("TRUST_PROXY", "true")
        resp = await client.post(
            "/api/plugins/example/ui/",
            headers={"X-Forwarded-For": "203.0.113.50"},
        )
        assert resp.status_code == 401
