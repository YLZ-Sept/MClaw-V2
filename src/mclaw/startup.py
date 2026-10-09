"""Lifecycle helpers for optional services that must not gate desktop chat.

IM 通道等"可选服务"的启动不能阻塞桌面聊天就绪：调用方把它包成后台任务，
就绪状态由 ``publish`` 回调在完成/失败时发布，而不是靠主启动路径 await。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)


async def run_optional_startup(
    start: Callable[[], Awaitable[Any]],
    publish: Callable[[Any, Exception | None], None],
) -> None:
    """Publish completion/failure, but never publish readiness after cancellation."""
    try:
        result = await start()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Optional service startup failed")
        publish(None, exc)
    else:
        publish(result, None)


async def cancel_startup_tasks(tasks: list[asyncio.Task]) -> None:
    """Drain startup coroutines before tearing down their runtime resources."""
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
