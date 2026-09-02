"""授权相关接口 ``/api/license/*``。

三个端点都在 license gate 的白名单里（否则未激活时无法激活），但**不在**
auth 白名单里——调用者必须先登录。``activate`` 进一步要求 admin 身份：
否则局域网内任何普通用户都能覆盖客户的授权码。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from mclaw.license.fingerprint import (
    FINGERPRINT_SEGMENTS,
    MIN_USABLE_SEGMENTS,
    collect_fingerprint,
    fingerprint_detail,
    usable_segment_count,
)
from mclaw.license.manager import LicenseManager
from mclaw.license.verifier import LicenseVerifyError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/license", tags=["授权"])


def _get_manager(request: Request) -> LicenseManager | None:
    return getattr(request.app.state, "license_manager", None)


def _require_admin(request: Request) -> str | None:
    """返回 admin 用户名；非 admin 返回 ``None``。

    与 ``routes/auth.py`` 的同名函数同源：依赖 auth 中间件填好的
    ``request.state.user_id``，没有 localhost 例外。
    """
    config = getattr(request.app.state, "web_access_config", None)
    username = getattr(request.state, "user_id", "") or ""
    if config is not None and username and config.is_admin(username):
        return username
    return None


@router.get("/status", summary="查询当前授权状态")
async def license_status(request: Request) -> JSONResponse:
    """返回授权状态。不含授权码原文。"""
    manager = _get_manager(request)
    if manager is None:
        # 授权系统未初始化（开发环境显式关闭时）——如实上报，不要假装已授权。
        return JSONResponse(
            content={
                "state": "disabled",
                "allows_access": True,
                "should_warn": False,
                "message": "授权系统未启用",
                "days_remaining": 0,
            }
        )
    return JSONResponse(content=manager.status.to_dict())


@router.get("/fingerprint", summary="读取本机硬件指纹")
async def license_fingerprint(request: Request) -> JSONResponse:
    """返回本机指纹，供客户发给供应商签发授权码。

    需登录但不需 admin——普通用户也可能负责联系供应商。指纹是单向哈希，
    泄露它不会暴露原始硬件信息，也不足以伪造授权码（仍需私钥签名）。
    """
    fingerprint = collect_fingerprint()
    detail = fingerprint_detail()
    usable = usable_segment_count(fingerprint)

    labels = {kind: label for kind, label in FINGERPRINT_SEGMENTS}
    return JSONResponse(
        content={
            "fingerprint": fingerprint,
            "usable_segments": usable,
            "total_segments": len(FINGERPRINT_SEGMENTS),
            "min_required": MIN_USABLE_SEGMENTS,
            "sufficient": usable >= MIN_USABLE_SEGMENTS,
            "components": [
                {"key": kind, "label": labels.get(kind, kind), "available": ok}
                for kind, ok in detail.items()
            ],
        }
    )


@router.post("/activate", summary="提交授权码激活（仅管理员）")
async def license_activate(request: Request) -> JSONResponse:
    """校验并保存授权码。

    校验失败时不写盘——避免一个坏码顶掉客户正在使用的好码。
    """
    if _require_admin(request) is None:
        return JSONResponse(
            status_code=403, content={"detail": "仅管理员可执行激活操作"}
        )

    manager = _get_manager(request)
    if manager is None:
        return JSONResponse(status_code=503, content={"detail": "授权系统未启用"})

    try:
        body = await request.json()
    except Exception:
        body = {}
    code = (body.get("code") or "").strip()
    if not code:
        return JSONResponse(status_code=400, content={"detail": "请填写授权码"})

    try:
        status = manager.activate(code)
    except LicenseVerifyError as exc:
        logger.warning("授权激活失败: %s", exc)
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    except OSError as exc:
        logger.error("授权码写入失败: %s", exc)
        return JSONResponse(
            status_code=500, content={"detail": f"授权码保存失败: {exc}"}
        )

    return JSONResponse(
        content={
            "status": "ok",
            # 插件/技能/IM 通道都在启动时加载，功能开关变更需重启后端。
            "restart_required": True,
            "license": status.to_dict(),
        }
    )
