"""蝉镜数字人插件 — 通过蝉镜 AI 开放平台生成语音（TTS）与数字人口播视频。

凭证来源：服务端环境变量 ``CHANJING_APP_ID`` / ``CHANJING_SECRET_KEY``（.env / .env.local）。
``secret_key`` 与 ``access_token`` 永不出现在日志或返回值中。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from mclaw.integrations.chanjing import ChanjingClient, ChanjingConfigError, ChanjingError
from mclaw.plugins.api import PluginAPI, PluginBase

from task_manager import TaskManager

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://open-api.chanjing.cc/open/v1"

# 轮询参数（固定下限 + 指数退避）
TTS_FIRST = 3.0
TTS_MAX = 10.0
TTS_TIMEOUT = 300.0
VIDEO_FIRST = 5.0
VIDEO_MAX = 15.0
VIDEO_TIMEOUT = 1200.0


def _pick_voice_id(voices: list[dict]) -> str | None:
    for v in voices:
        if v.get("name"):
            return v.get("id")
    return voices[0].get("id") if voices else None


class Plugin(PluginBase):
    def on_load(self, api: PluginAPI) -> None:
        self._api = api
        self._tm = TaskManager(api.get_data_dir() / "chanjing_dp.db")
        self._client: ChanjingClient | None = None
        self._client_sig: tuple[str, str, str] | None = None

        api.register_tools(self._tool_definitions(), handler=self._handle_tool)

        api.spawn_task(self._async_init(), name="chanjing-dp:init")
        api.log("Chanjing Digital Human plugin loaded")

    # ---- 生命周期与客户端 ----

    async def _async_init(self) -> None:
        await self._tm.init()

    async def _reset_client(self) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
            self._client_sig = None

    async def _get_client(self) -> ChanjingClient:
        cfg = await self._tm.get_config()
        app_id = (cfg.get("app_id") or "").strip()
        secret_key = (cfg.get("secret_key") or "").strip()
        base_url = (cfg.get("base_url") or "").strip() or DEFAULT_BASE_URL
        sig = (app_id, secret_key, base_url)
        if self._client is None or self._client_sig != sig:
            await self._reset_client()
            self._client = ChanjingClient(
                base_url=base_url,
                app_id=app_id or None,
                secret_key=secret_key or None,
            )
            await self._client.__aenter__()
            self._client_sig = sig
        return self._client

    # ---- 工具定义 ----

    def _tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "chanjing_list_audio",
                "description": "列出蝉镜开放平台可用的公共音色（含 id/name/gender/lang）。生成语音前用它选一个音色 id。",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "chanjing_list_dp",
                "description": "列出蝉镜开放平台可用的公共数字人（含 id/name/figures[type,width,height]）。合成视频前用它选数字人和形态。",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "chanjing_tts_create",
                "description": (
                    "用蝉镜把文本合成语音。返回 task_id，后台轮询；用 chanjing_tts_status 查进度，"
                    "成功后拿到音频 URL（audio_url）与时长（duration）。voice_id 留空则自动选一个可用音色。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "要合成的文本"},
                        "voice_id": {"type": "string", "description": "音色 id（可选，留空自动选）"},
                        "speed": {"type": "number", "description": "语速，默认 1"},
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "chanjing_tts_status",
                "description": "查询蝉镜 TTS 任务状态。成功（succeeded）后返回 audio_url 与 duration。",
                "input_schema": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
            {
                "name": "chanjing_video_create",
                "description": (
                    "用蝉镜把一段音频（chanjing_tts 产出的 audio_url）合成数字人口播视频。"
                    "返回 task_id，后台轮询；用 chanjing_video_status 查进度。"
                    "person_id/figure_type/width/height 任一留空则自动选一个可用数字人形态。"
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "audio_url": {"type": "string", "description": "TTS 结果音频 URL"},
                        "person_id": {"type": "string", "description": "数字人 id（可选）"},
                        "figure_type": {"type": "string", "description": "形态 type（可选）"},
                        "width": {"type": "integer", "description": "数字人宽（可选）"},
                        "height": {"type": "integer", "description": "数字人高（可选）"},
                    },
                    "required": ["audio_url"],
                },
            },
            {
                "name": "chanjing_video_status",
                "description": "查询蝉镜视频合成任务状态。成功（completed）后返回 video_url 与 duration。",
                "input_schema": {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                },
            },
        ]

    # ---- 工具处理器 ----

    async def _handle_tool(self, tool_name: str, args: dict) -> str:
        try:
            if tool_name == "chanjing_list_audio":
                result = await self._list_audio()
            elif tool_name == "chanjing_list_dp":
                result = await self._list_dp()
            elif tool_name == "chanjing_tts_create":
                result = await self._create_tts(
                    str(args.get("text") or ""),
                    str(args.get("voice_id") or ""),
                    float(args.get("speed") or 1.0),
                )
            elif tool_name == "chanjing_tts_status":
                result = await self._tts_status(str(args.get("task_id") or ""))
            elif tool_name == "chanjing_video_create":
                result = await self._create_video(
                    str(args.get("audio_url") or ""),
                    str(args.get("person_id") or ""),
                    str(args.get("figure_type") or ""),
                    int(args.get("width") or 0),
                    int(args.get("height") or 0),
                )
            elif tool_name == "chanjing_video_status":
                result = await self._video_status(str(args.get("task_id") or ""))
            else:
                result = {"ok": False, "error": f"unknown tool {tool_name}"}
        except ChanjingConfigError:
            result = {"ok": False, "error": "蝉镜凭证未配置，请先在设置页填写 app_id / secret_key"}
        except ChanjingError as e:
            result = {"ok": False, "error": str(e), "code": e.code, "trace_id": e.trace_id}
        except Exception as e:
            logger.warning(f"[chanjing-dp] tool {tool_name} failed: {e}")
            result = {"ok": False, "error": str(e)}
        return json.dumps(result, ensure_ascii=False)

    # ---- 业务方法（工具与路由共用） ----

    async def _list_audio(self) -> dict:
        client = await self._get_client()
        resp = await client.list_common_audio(page=1, size=50)
        return {"ok": True, "list": (resp.get("data") or {}).get("list") or []}

    async def _list_dp(self) -> dict:
        client = await self._get_client()
        resp = await client.list_common_dp(page=1, size=50)
        return {"ok": True, "list": (resp.get("data") or {}).get("list") or []}

    async def _pick_dp(self) -> tuple[dict | None, dict | None]:
        dps = (await self._list_dp()).get("list") or []
        for dp in dps:
            if not dp.get("name"):
                continue
            for fig in dp.get("figures") or []:
                if fig.get("type") and fig.get("width") and fig.get("height"):
                    return dp, fig
        return None, None

    async def _create_tts(self, text: str, voice_id: str, speed: float) -> dict:
        if not text.strip():
            return {"ok": False, "error": "text 不能为空"}
        client = await self._get_client()
        if not voice_id:
            voices = (await self._list_audio()).get("list") or []
            voice_id = _pick_voice_id(voices) or ""
            if not voice_id:
                return {"ok": False, "error": "没有可用音色"}
        resp = await client.create_audio_task(
            audio_man=voice_id, speed=speed or 1.0, text=text, plain_text=text
        )
        tts_task_id = (resp.get("data") or {}).get("task_id")
        if not tts_task_id:
            return {"ok": False, "error": "未返回 task_id"}
        await self._tm.create_task(tts_task_id, "tts", upstream_id=tts_task_id)
        self._api.spawn_task(self._poll_tts(tts_task_id), name=f"chanjing-dp:tts:{tts_task_id}")
        return {"ok": True, "task_id": tts_task_id, "voice_id": voice_id, "status": "pending"}

    async def _tts_status(self, task_id: str) -> dict:
        if not task_id:
            return {"ok": False, "error": "task_id 不能为空"}
        task = await self._tm.get_task(task_id)
        if task and task["status"] in ("succeeded", "failed"):
            return {
                "ok": task["status"] == "succeeded",
                "task_id": task_id,
                "status": task["status"],
                "audio_url": task["audio_url"],
                "duration": task["duration"],
                "error_message": task["error_message"],
                "trace_id": task["trace_id"],
            }
        return await self._tts_probe(task_id)

    async def _tts_probe(self, task_id: str) -> dict:
        client = await self._get_client()
        resp = await client.audio_task_state(task_id)
        data = resp.get("data") or {}
        err = data.get("errMsg") or data.get("errReason")
        full = data.get("full") or {}
        if err:
            return {"ok": False, "task_id": task_id, "status": "failed",
                    "error_message": err, "trace_id": resp.get("trace_id")}
        if data.get("status") == 9 and full.get("url"):
            await self._tm.create_task(task_id, "tts", upstream_id=task_id)
            await self._tm.update_task(task_id, status="succeeded", audio_url=full.get("url"),
                                       duration=full.get("duration"), trace_id=resp.get("trace_id"))
            return {"ok": True, "task_id": task_id, "status": "succeeded",
                    "audio_url": full.get("url"), "duration": full.get("duration")}
        return {"ok": True, "task_id": task_id, "status": "running"}

    async def _create_video(
        self, audio_url: str, person_id: str, figure_type: str, width: int, height: int
    ) -> dict:
        if not audio_url.strip():
            return {"ok": False, "error": "audio_url 不能为空"}
        client = await self._get_client()
        if not (person_id and figure_type and width and height):
            dp, fig = await self._pick_dp()
            if dp is None:
                return {"ok": False, "error": "没有可用数字人"}
            person_id = dp.get("id")
            figure_type = fig.get("type")
            width = fig.get("width")
            height = fig.get("height")
        resp = await client.create_video(
            person={"id": person_id, "figure_type": figure_type, "width": width, "height": height},
            audio={"type": "audio", "wav_url": audio_url, "volume": 100},
            screen_width=width,
            screen_height=height,
        )
        video_task_id = resp.get("data")  # 字符串，不是对象
        if not video_task_id:
            return {"ok": False, "error": "未返回任务 id"}
        await self._tm.create_task(video_task_id, "video", upstream_id=video_task_id)
        self._api.spawn_task(
            self._poll_video(video_task_id), name=f"chanjing-dp:video:{video_task_id}"
        )
        return {"ok": True, "task_id": video_task_id, "person_id": person_id, "status": "pending"}

    async def _video_status(self, task_id: str) -> dict:
        if not task_id:
            return {"ok": False, "error": "task_id 不能为空"}
        task = await self._tm.get_task(task_id)
        if task and task["status"] in ("succeeded", "failed"):
            return {
                "ok": task["status"] == "succeeded",
                "task_id": task_id,
                "status": task["status"],
                "video_url": task["video_url"],
                "duration": task["duration"],
                "error_message": task["error_message"],
                "trace_id": task["trace_id"],
            }
        return await self._video_probe(task_id)

    async def _video_probe(self, task_id: str) -> dict:
        client = await self._get_client()
        resp = await client.get_video(task_id)
        data = resp.get("data") or {}
        qs = data.get("queue_status")
        if qs == "completed" and data.get("video_url"):
            await self._tm.create_task(task_id, "video", upstream_id=task_id)
            await self._tm.update_task(task_id, status="succeeded", video_url=data.get("video_url"),
                                       duration=data.get("duration"), trace_id=resp.get("trace_id"))
            return {"ok": True, "task_id": task_id, "status": "succeeded",
                    "video_url": data.get("video_url"), "duration": data.get("duration")}
        if qs == "failed":
            return {"ok": False, "task_id": task_id, "status": "failed",
                    "error_message": data.get("msg"), "trace_id": resp.get("trace_id")}
        if qs == "other":
            return {"ok": False, "task_id": task_id, "status": "other",
                    "error_message": f"queue_status=other msg={data.get('msg')} queue_desc={data.get('queue_desc')}",
                    "trace_id": resp.get("trace_id")}
        return {"ok": True, "task_id": task_id, "status": "running",
                "queue_desc": data.get("queue_desc")}

    # ---- 后台轮询 ----

    def _broadcast(self, payload: dict) -> None:
        try:
            self._api.broadcast_ui_event("task_update", payload)
        except Exception:
            pass

    async def _poll_tts(self, task_id: str) -> None:
        try:
            client = await self._get_client()
        except Exception as e:
            await self._tm.update_task(task_id, status="failed", error_message=str(e))
            return
        deadline = time.monotonic() + TTS_TIMEOUT
        delay = TTS_FIRST
        while True:
            try:
                resp = await client.audio_task_state(task_id)
            except Exception as e:
                await self._tm.update_task(task_id, status="failed", error_message=str(e))
                self._broadcast({"task_id": task_id, "status": "failed"})
                return
            data = resp.get("data") or {}
            err = data.get("errMsg") or data.get("errReason")
            full = data.get("full") or {}
            if err:
                await self._tm.update_task(task_id, status="failed", error_message=err,
                                           trace_id=resp.get("trace_id"))
                self._broadcast({"task_id": task_id, "status": "failed"})
                return
            if data.get("status") == 9 and full.get("url"):
                await self._tm.update_task(task_id, status="succeeded", audio_url=full.get("url"),
                                           duration=full.get("duration"), trace_id=resp.get("trace_id"))
                self._broadcast({"task_id": task_id, "status": "succeeded", "audio_url": full.get("url")})
                return
            await self._tm.update_task(task_id, status="running")
            if time.monotonic() >= deadline:
                await self._tm.update_task(task_id, status="failed", error_message="轮询超时")
                self._broadcast({"task_id": task_id, "status": "failed"})
                return
            await asyncio.sleep(delay)
            delay = min(TTS_MAX, delay * 2)

    async def _poll_video(self, task_id: str) -> None:
        try:
            client = await self._get_client()
        except Exception as e:
            await self._tm.update_task(task_id, status="failed", error_message=str(e))
            return
        deadline = time.monotonic() + VIDEO_TIMEOUT
        delay = VIDEO_FIRST
        while True:
            try:
                resp = await client.get_video(task_id)
            except Exception as e:
                await self._tm.update_task(task_id, status="failed", error_message=str(e))
                self._broadcast({"task_id": task_id, "status": "failed"})
                return
            data = resp.get("data") or {}
            qs = data.get("queue_status")
            if qs == "completed" and data.get("video_url"):
                await self._tm.update_task(task_id, status="succeeded", video_url=data.get("video_url"),
                                           duration=data.get("duration"), trace_id=resp.get("trace_id"))
                self._broadcast({"task_id": task_id, "status": "succeeded", "video_url": data.get("video_url")})
                return
            if qs == "failed":
                await self._tm.update_task(task_id, status="failed", error_message=data.get("msg"),
                                           trace_id=resp.get("trace_id"))
                self._broadcast({"task_id": task_id, "status": "failed"})
                return
            if qs == "other":
                await self._tm.update_task(
                    task_id, status="failed",
                    error_message=f"queue_status=other msg={data.get('msg')} queue_desc={data.get('queue_desc')}",
                    trace_id=resp.get("trace_id"),
                )
                self._broadcast({"task_id": task_id, "status": "failed"})
                return
            await self._tm.update_task(task_id, status="running")
            if time.monotonic() >= deadline:
                await self._tm.update_task(task_id, status="failed", error_message="轮询超时")
                self._broadcast({"task_id": task_id, "status": "failed"})
                return
            await asyncio.sleep(delay)
            delay = min(VIDEO_MAX, delay * 2)
