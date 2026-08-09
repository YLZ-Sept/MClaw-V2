"""
Web access authentication for Mclaw.

Single-password mode with JWT tokens. Local requests (127.0.0.1) are exempt
from authentication to preserve the desktop experience.

Storage: data/web_access.json
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..core.auth.tokens import TokenClaims, decode_jwt, encode_jwt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACCESS_TOKEN_TTL = 24 * 3600  # 24 hours
REFRESH_TOKEN_TTL = 90 * 24 * 3600  # 90 days
REFRESH_COOKIE_NAME = "mclaw_refresh"
PASSWORD_ENV_VAR = "OPENAKITA_WEB_PASSWORD"

AUTH_EXEMPT_PATHS = frozenset(
    {
        "/",
        "/api/health",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/refresh",
        "/api/auth/check",
        "/api/auth/setup",
        "/api/auth/setup-status",
        "/api/logs/frontend",
    }
)
AUTH_EXEMPT_PREFIXES = ("/web/", "/web", "/ws/", "/docs", "/openapi.json", "/redoc", "/user-docs")
PLUGIN_UI_ASSET_PATH = re.compile(r"^/api/plugins/[^/]+/ui(?:/.*)?$")

# ---------------------------------------------------------------------------
# Password hashing (scrypt, stdlib)
# ---------------------------------------------------------------------------


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hash password with scrypt. Returns (hash_hex, salt_hex)."""
    if salt is None:
        salt = secrets.token_bytes(16)
    h = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=16384,
        r=8,
        p=1,
        dklen=32,
    )
    return h.hex(), salt.hex()


def _verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    try:
        h = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt_hex),
            n=16384,
            r=8,
            p=1,
            dklen=32,
        )
        return hmac.compare_digest(h.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Web Access config (data/web_access.json)
# ---------------------------------------------------------------------------


class WebAccessConfig:
    r"""Manages the web_access.json file with multi-user support.

    Format (v2):
      {
        "jwt_secret": "...",
        "data_epoch": "...",
        "token_version": 1,
        "users": {
          "admin": {"password_hash": "...", "password_salt": "...", "role": "admin"},
          "zhangsan": {"password_hash": "...", "password_salt": "...", "role": "user"}
        }
      }

    Backward-compatible with v1 single-password format — auto-migrates on load.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "web_access.json"
        self._data: dict[str, Any] = {}
        self._lock = __import__("threading").Lock()
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text("utf-8"))
            except Exception:
                logger.error(
                    "Failed to read %s — file appears corrupted; "
                    "regenerating fresh config (any saved password will be lost)",
                    self._path,
                    exc_info=True,
                )
                self._data = {}

        env_password = os.environ.get(PASSWORD_ENV_VAR, "").strip()
        needs_save = False

        # Ensure base fields
        if not self._data.get("jwt_secret"):
            self._data["jwt_secret"] = secrets.token_hex(32)
            needs_save = True
        if not self._data.get("data_epoch"):
            self._data["data_epoch"] = secrets.token_hex(8)
            needs_save = True
        if not self._data.get("token_version"):
            self._data["token_version"] = 1
            needs_save = True

        # Migrate v1 single-password format → v2 users dict
        if "password_hash" in self._data and "users" not in self._data:
            self._data["users"] = {
                "admin": {
                    "password_hash": self._data.pop("password_hash"),
                    "password_salt": self._data.pop("password_salt"),
                    "role": "admin",
                }
            }
            self._data.pop("password_plain_hint", None)
            self._data.pop("password_user_set", None)
            needs_save = True
            logger.info("Migrated web_access.json from v1 (single-password) to v2 (multi-user)")

        if "users" not in self._data:
            self._data["users"] = {}

        # Env-var password → admin user
        if env_password:
            admin = self._data["users"].get("admin", {})
            existing_hash = admin.get("password_hash", "")
            existing_salt = admin.get("password_salt", "")
            if (
                not existing_hash
                or not existing_salt
                or not _verify_password(env_password, existing_hash, existing_salt)
            ):
                hash_hex, salt_hex = _hash_password(env_password)
                self._data["users"]["admin"] = {
                    "password_hash": hash_hex,
                    "password_salt": salt_hex,
                    "role": "admin",
                }
                needs_save = True

        if needs_save:
            self._data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._save()

    def _save(self) -> None:
        """Persist ``self._data`` to disk atomically and durably.

        Sequence: write to ``*.tmp`` → ``flush`` + ``fsync`` the file →
        ``os.replace`` for atomic swap → ``fsync`` parent dir (POSIX only).
        This protects against power loss between bytes-flush and rename, which
        is the most common cause of ``web_access.json`` corruption reports.
        Windows does not support directory ``fsync`` but ``os.replace`` is
        atomic on NTFS so the rename itself is durable enough.
        """
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            payload = json.dumps(self._data, indent=2) + "\n"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
            if os.name == "posix":
                try:
                    dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError as exc:
                    logger.warning(
                        "Failed to fsync data dir %s: %s",
                        self._path.parent,
                        exc,
                    )

    @property
    def jwt_secret(self) -> str:
        return self._data["jwt_secret"]

    @property
    def token_version(self) -> int:
        return self._data.get("token_version", 1)

    @property
    def data_epoch(self) -> str:
        return self._data.get("data_epoch", "")

    # ── User registry ────────────────────────────────────────────────────

    @property
    def _users(self) -> dict[str, Any]:
        return self._data.get("users", {})

    def verify_password(self, username: str, password: str) -> bool:
        """Verify username + password. Also accepts old single-password API calls."""
        user = self._users.get(username)
        if user:
            return _verify_password(password, user.get("password_hash", ""),
                                    user.get("password_salt", ""))
        return False

    @property
    def password_user_set(self) -> bool:
        return len(self._users) > 0

    @property
    def has_password_set(self) -> bool:
        return len(self._users) > 0

    def is_admin(self, username: str) -> bool:
        user = self._users.get(username)
        return user is not None and user.get("role") == "admin"

    def list_users(self) -> list[dict[str, Any]]:
        return [
            {"username": name, "role": info.get("role", "user")}
            for name, info in self._users.items()
        ]

    def add_user(self, username: str, password: str, role: str = "user") -> None:
        username = username.strip().lower()
        if not username or not re.match(r"^[a-z0-9_-]+$", username):
            raise ValueError("用户名仅允许小写字母、数字、下划线和连字符")
        if username in self._users:
            raise ValueError(f"用户 '{username}' 已存在")
        hash_hex, salt_hex = _hash_password(password)
        self._data["users"][username] = {
            "password_hash": hash_hex,
            "password_salt": salt_hex,
            "role": role,
        }
        self._data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()

    def remove_user(self, username: str) -> None:
        username = username.strip().lower()
        if username not in self._users:
            raise ValueError(f"用户 '{username}' 不存在")
        if len(self._users) <= 1:
            raise ValueError("不能删除最后一个用户")
        self._data["users"].pop(username, None)
        self._data["token_version"] = self.token_version + 1
        self._data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()

    def change_password(self, username: str, new_password: str) -> None:
        username = username.strip().lower()
        if username not in self._users:
            raise ValueError(f"用户 '{username}' 不存在")
        hash_hex, salt_hex = _hash_password(new_password)
        self._data["users"][username]["password_hash"] = hash_hex
        self._data["users"][username]["password_salt"] = salt_hex
        self._data["token_version"] = self.token_version + 1
        self._data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()

    def admin_reset_password(self, username: str, new_password: str) -> None:
        """Admin resets any user's password (no old password needed)."""
        self.change_password(username, new_password)

    def clear_password(self) -> None:
        self._data.pop("users", None)
        self._data["users"] = {}
        self._data["token_version"] = self.token_version + 1
        self._data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._save()

    # ── Tokens ────────────────────────────────────────────────────────────

    def _get_user_claims(self, username: str) -> dict[str, Any]:
        user = self._users.get(username, {})
        return {"role": user.get("role", "user")}

    def create_access_token(self, username: str = "desktop_user") -> str:
        claims = TokenClaims(
            token_type="access",
            subject=username,
            expires_in=ACCESS_TOKEN_TTL,
            version=self.token_version,
            scope=["web:access"],
        )
        payload = claims.to_payload()
        payload["user"] = self._get_user_claims(username)
        return encode_jwt(payload, self.jwt_secret)

    def create_refresh_token(self, username: str = "desktop_user") -> str:
        claims = TokenClaims(
            token_type="refresh",
            subject=username,
            expires_in=REFRESH_TOKEN_TTL,
            version=self.token_version,
            scope=["web:refresh"],
        )
        return encode_jwt(claims.to_payload(), self.jwt_secret)

    def validate_access_token(self, token: str) -> bool:
        payload = decode_jwt(token, self.jwt_secret)
        if not payload:
            return False
        if payload.get("type") != "access":
            return False
        if payload.get("ver") != self.token_version:
            return False
        return True

    def get_token_subject(self, token: str) -> str | None:
        """Extract the username (subject) from a valid access token."""
        payload = decode_jwt(token, self.jwt_secret)
        if not payload:
            return None
        return payload.get("sub")

    def validate_refresh_token(self, token: str) -> dict[str, Any] | None:
        payload = decode_jwt(token, self.jwt_secret)
        if not payload:
            return None
        if payload.get("type") != "refresh":
            return None
        if payload.get("ver") != self.token_version:
            return None
        return payload


def _make_hint(password: str) -> str:
    if len(password) <= 6:
        return password[0] + "..." + password[-1] if len(password) >= 2 else "***"
    return password[:3] + "..." + password[-3:]


# ---------------------------------------------------------------------------
# Rate limiter (simple in-memory, per-IP)
# ---------------------------------------------------------------------------


class RateLimiter:
    """Simple sliding-window rate limiter.

    Tracks the timestamps of *failures* (the caller decides what counts as
    a failure by being explicit about :meth:`register_failure` vs
    :meth:`clear`). :meth:`is_allowed` returns False when the most recent
    window already contains the cap, and :meth:`retry_after_seconds` tells
    the HTTP layer how long until the oldest hit ages out so a meaningful
    ``Retry-After`` header can be returned.
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}

    def _trim(self, key: str, now: float) -> list[float]:
        timestamps = [t for t in self._hits.get(key, []) if now - t < self._window]
        if timestamps:
            self._hits[key] = timestamps
        else:
            self._hits.pop(key, None)
        return timestamps

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        return len(self._trim(key, now)) < self._max

    def register_failure(self, key: str) -> None:
        now = time.time()
        timestamps = self._trim(key, now)
        timestamps.append(now)
        self._hits[key] = timestamps

    def clear(self, key: str) -> None:
        self._hits.pop(key, None)

    def retry_after_seconds(self, key: str) -> int:
        now = time.time()
        timestamps = self._trim(key, now)
        if not timestamps:
            return 0
        oldest = timestamps[0]
        return max(1, int(self._window - (now - oldest)))


# Global rate limiters
# 5 failed logins / 5 minutes is the canonical "annoy a bot, don't annoy a
# user" setting. The previous 5/60s was too aggressive (a legitimate user
# trying three wrong passwords could lock themselves out for a minute);
# extending the window to 5 minutes while keeping the count gives the user
# the same number of tries but discourages credential-stuffing scripts.
_login_limiter = RateLimiter(max_requests=5, window_seconds=300)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def get_client_ip(request: Request, *, trust_proxy: bool = False) -> str:
    """Return the client IP, respecting X-Forwarded-For when trust_proxy is on."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_local_request(request: Request) -> bool:
    """Check if request originates from localhost (direct connection only).

    Handles plain IPv4/IPv6 loopback as well as IPv4-mapped IPv6 addresses
    (``::ffff:127.0.0.1``) which some OS/Uvicorn combinations report when the
    server binds to ``0.0.0.0`` on dual-stack systems (common on Windows).
    """
    if not request.client:
        return False
    host = request.client.host
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    # IPv4-mapped IPv6: ::ffff:127.0.0.1
    if host.startswith("::ffff:") and host[7:] == "127.0.0.1":
        return True
    return False


def is_trusted_local(request: Request) -> bool:
    """Return True when the request is a direct local connection.

    A direct local connection is one that:

    - originates from a loopback address (see :func:`_is_local_request`), and
    - is *not* being forwarded by a reverse proxy.

    The second clause matters under ``TRUST_PROXY=true``: an Nginx/Caddy in
    front of the server will *itself* connect from 127.0.0.1 but always sets
    ``X-Forwarded-For``. A direct local connection (e.g. Tauri / curl on the
    same host) comes from 127.0.0.1 with *no* ``X-Forwarded-For`` header.
    We use that distinction to tell the two apart so that proxied requests
    are still subject to authentication / setup gating.

    Shared by :func:`create_auth_middleware` and
    :mod:`mclaw.api.setup_state` — keep them aligned by going through
    this single helper.
    """
    if not _is_local_request(request):
        return False
    trust_proxy = os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes")
    if trust_proxy and request.headers.get("x-forwarded-for"):
        return False
    return True


def _is_auth_exempt(path: str) -> bool:
    """Check if the path is exempt from authentication."""
    if path in AUTH_EXEMPT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in AUTH_EXEMPT_PREFIXES)


def _is_public_plugin_ui_asset(request: Request) -> bool:
    """Allow iframe documents and their static assets without exposing plugin APIs."""
    return request.method in {"GET", "HEAD"} and bool(
        PLUGIN_UI_ASSET_PATH.fullmatch(request.url.path)
    )


def create_auth_middleware(config: WebAccessConfig):
    """Create the authentication middleware function.

    On successful auth, sets ``request.state.user_id`` to the authenticated
    username so downstream routes can scope data per user.
    """

    async def auth_middleware(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        if _is_auth_exempt(path) or _is_public_plugin_ui_asset(request):
            return await call_next(request)

        # Bearer token
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            if config.validate_access_token(token):
                user_id = config.get_token_subject(token) or "desktop_user"
                request.state.user_id = user_id
                request.state.token = token
                return await call_next(request)

        # Query token
        query_token = request.query_params.get("token", "")
        if query_token and config.validate_access_token(query_token):
            user_id = config.get_token_subject(query_token) or "desktop_user"
            request.state.user_id = user_id
            return await call_next(request)

        # X-API-Key (programmatic) — try verify against admin user
        api_key = request.headers.get("x-api-key", "")
        if api_key and config.verify_password("admin", api_key):
            request.state.user_id = "admin"
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
        )

    return auth_middleware
