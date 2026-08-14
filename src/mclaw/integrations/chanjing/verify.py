"""蝉镜 AI 开放平台接入验收：独立 TTS → 数字人视频。

运行前请确保服务端环境变量已配置（放项目根 ``.env`` 或 ``.env.local``，
两者均已 .gitignore）：

    CHANJING_APP_ID=...
    CHANJING_SECRET_KEY=...

运行：

    python -m mclaw.integrations.chanjing.verify

验收通过需同时满足：
    1) 音频 URL 与视频 URL 实际可访问（HTTP 200 且 Content-Length 非 0）；
    2) 视频 data.duration 与 TTS data.full.duration 接近（允许 1~2 秒误差）；
    3) 两个 URL 均非永久存储，需尽快转存。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .client import ChanjingClient, ChanjingError

TEXT = "这是一条蝉镜开放平台接入验收语音。"

# 轮询参数（固定下限 + 指数退避）
TTS_FIRST = 3.0
TTS_MAX = 10.0
TTS_TIMEOUT = 300.0  # 5 分钟

VIDEO_FIRST = 5.0
VIDEO_MAX = 15.0
VIDEO_TIMEOUT = 1200.0  # 20 分钟


def _log(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def _pick_voice(audio_list: list) -> dict:
    """从音色列表选一个 name 有明确含义的音色（不盲取 list[0]）。"""
    if not audio_list:
        raise RuntimeError("音色列表为空")
    for item in audio_list:
        if item.get("name"):
            return item
    return audio_list[0]


def _pick_dp(dp_list: list) -> tuple[dict, dict]:
    """从数字人列表选一个 name 有意义、且含有效形态(type/width/height)的数字人。"""
    if not dp_list:
        raise RuntimeError("数字人列表为空")
    for item in dp_list:
        if not item.get("name"):
            continue
        figures = item.get("figures") or []
        for fig in figures:
            if fig.get("type") and fig.get("width") and fig.get("height"):
                return item, fig
    raise RuntimeError("未找到含有效形态(type/width/height)的数字人")


async def _poll_audio(client: ChanjingClient, task_id: str) -> tuple[dict, str | None]:
    """轮询 TTS 状态。成功返回 (data, trace_id)。"""
    deadline = time.monotonic() + TTS_TIMEOUT
    delay = TTS_FIRST
    while True:
        resp = await client.audio_task_state(task_id)
        trace_id = resp.get("trace_id")
        data = resp.get("data") or {}
        status = data.get("status")
        err_msg = data.get("errMsg")
        err_reason = data.get("errReason")
        full = data.get("full") or {}

        if err_msg or err_reason:
            raise ChanjingError(
                "/audio_task_state", 0, f"errMsg={err_msg!r} errReason={err_reason!r}", trace_id
            )
        if status == 9 and full.get("url"):
            return data, trace_id
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"TTS 轮询超时：status={status} errMsg={err_msg!r} "
                f"errReason={err_reason!r} trace_id={trace_id}"
            )
        await asyncio.sleep(delay)
        delay = min(TTS_MAX, delay * 2)


async def _poll_video(client: ChanjingClient, task_id: str) -> tuple[dict, str | None]:
    """轮询视频状态。成功返回 (data, trace_id)。"""
    deadline = time.monotonic() + VIDEO_TIMEOUT
    delay = VIDEO_FIRST
    while True:
        resp = await client.get_video(task_id)
        trace_id = resp.get("trace_id")
        data = resp.get("data") or {}
        queue_status = data.get("queue_status")

        if queue_status == "completed" and data.get("video_url"):
            return data, trace_id
        if queue_status == "failed":
            raise ChanjingError("/video", 0, f"failed msg={data.get('msg')!r}", trace_id)
        if queue_status == "other":
            raise ChanjingError(
                "/video",
                0,
                f"other queue_status={queue_status!r} msg={data.get('msg')!r} "
                f"queue_desc={data.get('queue_desc')!r}",
                trace_id,
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"视频轮询超时：queue_status={queue_status} msg={data.get('msg')!r} "
                f"queue_desc={data.get('queue_desc')!r} trace_id={trace_id}"
            )
        await asyncio.sleep(delay)
        delay = min(VIDEO_MAX, delay * 2)


async def _probe_url(url: str) -> dict:
    """探测 URL 是否可访问并返回内容大小（不下载完整内容）。"""
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as c:
        resp = await c.get(url, headers={"Range": "bytes=0-0"})
        size: int | None = None
        content_range = resp.headers.get("Content-Range", "")
        if "/" in content_range:
            total = content_range.rsplit("/", 1)[-1]
            if total.isdigit():
                size = int(total)
        if size is None:
            cl = resp.headers.get("Content-Length", "")
            if cl.isdigit():
                size = int(cl)
        return {
            "status": resp.status_code,
            "size": size,
            "content_length": resp.headers.get("Content-Length"),
        }


async def main() -> int:
    print("=" * 64)
    print("蝉镜 AI 开放平台接入验收：独立 TTS → 数字人视频")
    print("=" * 64)

    audio_url = ""
    audio_duration: int | float | None = None
    video_url = ""
    video_duration: int | float | None = None

    async with ChanjingClient() as client:
        # 1. access_token
        _log("1", "POST /access_token")
        token_info = await client.access_token()
        _log("1", f"OK  code=0  expire_in={token_info['expire_in']}s")

        # 2. list_common_audio
        _log("2", "GET /list_common_audio?page=1&size=20")
        audio_resp = await client.list_common_audio(page=1, size=20)
        audio_list = (audio_resp.get("data") or {}).get("list") or []
        voice = _pick_voice(audio_list)
        voice_id = voice.get("id")
        _log("2", f"OK  code=0  trace_id={audio_resp.get('trace_id')}  选择音色 id={voice_id} name={voice.get('name')!r}")

        # 3. create_audio_task
        _log("3", "POST /create_audio_task")
        task_resp = await client.create_audio_task(
            audio_man=voice_id, speed=1, text=TEXT, plain_text=TEXT
        )
        tts_task_id = (task_resp.get("data") or {}).get("task_id")
        _log("3", f"OK  code=0  trace_id={task_resp.get('trace_id')}  task_id={tts_task_id}")

        # 4. audio_task_state 轮询
        _log("4", "POST /audio_task_state 轮询 ...")
        audio_state, audio_trace = await _poll_audio(client, tts_task_id)
        full = audio_state.get("full") or {}
        audio_url = full.get("url") or ""
        audio_duration = full.get("duration")
        _log("4", f"OK  status=9  duration={audio_duration}s  trace_id={audio_trace}")

        # 5. list_common_dp
        _log("5", "GET /list_common_dp?page=1&size=20")
        dp_resp = await client.list_common_dp(page=1, size=20)
        dp_list = (dp_resp.get("data") or {}).get("list") or []
        dp, figure = _pick_dp(dp_list)
        _log(
            "5",
            f"OK  code=0  trace_id={dp_resp.get('trace_id')}  选择数字人 id={dp.get('id')} "
            f"name={dp.get('name')!r}  figure(type={figure.get('type')}, "
            f"{figure.get('width')}x{figure.get('height')})",
        )

        # 6. create_video
        _log("6", "POST /create_video")
        video_resp = await client.create_video(
            person={
                "id": dp.get("id"),
                "figure_type": figure.get("type"),
                "width": figure.get("width"),
                "height": figure.get("height"),
            },
            audio={"type": "audio", "wav_url": audio_url, "volume": 100},
            screen_width=figure.get("width"),
            screen_height=figure.get("height"),
        )
        video_task_id = video_resp.get("data")  # 字符串，不是对象
        _log("6", f"OK  code=0  trace_id={video_resp.get('trace_id')}  task_id={video_task_id}")

        # 7. video 轮询
        _log("7", "GET /video 轮询 ...")
        video_state, video_trace = await _poll_video(client, video_task_id)
        video_url = video_state.get("video_url") or ""
        video_duration = video_state.get("duration")
        _log("7", f"OK  queue_status=completed  duration={video_duration}s  trace_id={video_trace}")

    # 8. 验收
    print()
    _log("8", "验收检查")
    audio_probe = await _probe_url(audio_url)
    video_probe = await _probe_url(video_url)

    audio_ok = audio_probe["status"] in (200, 206) and bool(audio_probe["size"])
    video_ok = video_probe["status"] in (200, 206) and bool(video_probe["size"])

    dur_ok = False
    dur_note = ""
    if audio_duration is not None and video_duration is not None:
        diff = abs(float(video_duration) - float(audio_duration))
        dur_ok = diff <= 2.0
        dur_note = f"音频 {audio_duration}s vs 视频 {video_duration}s，差 {diff:.2f}s"
    else:
        dur_note = f"音频 {audio_duration} vs 视频 {video_duration}（缺失，无法比较）"

    print(f"[8] 音频 URL 可访问: {'PASS' if audio_ok else 'FAIL'}  status={audio_probe['status']} size={audio_probe['size']}")
    print(f"[8] 视频 URL 可访问: {'PASS' if video_ok else 'FAIL'}  status={video_probe['status']} size={video_probe['size']}")
    print(f"[8] 视频/音频时长接近: {'PASS' if dur_ok else 'FAIL'}  {dur_note}")

    print()
    print("---- 结果 ----")
    print(f"音频 URL: {audio_url}")
    print(f"视频 URL: {video_url}")
    print()
    print("注意：以上两个 URL 非永久存储，请尽快转存到自有存储。")

    passed = audio_ok and video_ok and dur_ok
    print()
    print(f"验收结论: {'通过' if passed else '未通过'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
