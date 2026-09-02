"""FastAPI 中间件：拦截未授权的业务请求。

当系统未激活、授权码失效或已过宽限期时，所有业务接口返回 HTTP 402，
前端据此切换到激活页。

**注册顺序**：本中间件必须在入站方向上运行于
:func:`mclaw.api.auth.create_auth_middleware` **之后**——FastAPI 中间件栈
是 LIFO（后注册的先执行），因此本中间件要**最先注册**。

排在 auth 之后是刻意的：激活接口须要求 admin 身份，否则局域网内任何人都
能覆盖客户的授权码或读取机器指纹。届时 ``request.state.user_id`` 已由
auth 中间件填好。

策略判定全部委托给 :class:`~mclaw.license.manager.LicenseManager`；本模块
只负责路径白名单与 HTTP 响应信封。
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from mclaw.license.manager import LicenseManager

logger = logging.getLogger(__name__)


# 未激活时也必须可达的精确路径。
#
# ``/api/license/*`` 三个接口是激活流程本身——放行它们才能让客户看到指纹、
# 提交授权码。注意它们**不在** auth 白名单里：调用者仍须先登录，
# ``activate`` 还要求 admin。
#
# ``/api/auth/*`` 全部放行：登录/登出/改密码在未激活时必须可用，否则客户
# 连激活页都进不去。
LICENSE_GATE_ALLOW_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/api/health",
        "/api/healthz",
        "/api/readyz",
        "/api/license/status",
        "/api/license/fingerprint",
        "/api/license/activate",
        "/api/logs/frontend",
    }
)

LICENSE_GATE_ALLOW_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
    "/web/",
    "/web",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/user-docs",
    "/static/",
)

# 状态 → HTTP 响应体中的 error 码。前端按此分支给出不同引导文案。
_ERROR_CODES = {
    "missing": "license_required",
    "invalid": "license_invalid",
    "mismatch": "license_mismatch",
    "expired": "license_expired",
}


def _is_allowed_path(path: str) -> bool:
    if path in LICENSE_GATE_ALLOW_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in LICENSE_GATE_ALLOW_PREFIXES)


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/")


def create_license_gate_middleware(manager: LicenseManager):
    """构造绑定到指定 :class:`LicenseManager` 的中间件闭包。

    闭包按引用捕获 ``manager``，因此运行时激活成功后无需重建中间件即可
    立即放行。
    """

    async def license_gate_middleware(request: Request, call_next):
        # CORS 预检无条件放行。
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        if _is_allowed_path(path):
            return await call_next(request)

        # 非 API 路径（SPA 导航、静态资源）交给下游处理——前端自己会探
        # /api/license/status 并切到激活页。
        if not _is_api_path(path):
            return await call_next(request)

        status = manager.status
        if status.allows_access:
            # 授权有效：更新时钟水位线（内部按小时节流，绝大多数调用只是
            # 比较一个内存时间戳后立即返回），并把授权内容挂到 request 上
            # 供业务读取 features / max_users。
            manager.touch_heartbeat()
            request.state.license = status.payload
            return await call_next(request)

        error_code = _ERROR_CODES.get(status.state.value, "license_required")
        logger.info(
            "license_gate blocked %s %s from %s — %s",
            request.method,
            path,
            request.client.host if request.client else "unknown",
            status.state.value,
        )
        return JSONResponse(
            status_code=402,
            content={
                "error": error_code,
                "detail": status.message,
                "state": status.state.value,
                "license_url": "/web/#/license",
            },
        )

    return license_gate_middleware
