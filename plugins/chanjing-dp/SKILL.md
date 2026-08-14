---
name: chanjing-dp
description: 通过蝉镜 AI 开放平台生成语音（TTS）与数字人口播视频。任务异步执行 + 轮询，成功后返回音频/视频 URL（非永久，需转存）。
env_any:
  - CHANJING_APP_ID
  - CHANJING_SECRET_KEY
---

# 蝉镜数字人 / Chanjing Digital Human

## 是什么 / What

输入文本，先用蝉镜合成语音（TTS），再把这段音频驱动一个数字人合成口播视频。
流程分两段：**TTS**（快，几秒）→ **视频合成**（慢，分钟级）。均为异步任务：先创建拿 task_id，再轮询状态。

## 何时用 / When

- 用户想要"一段 AI 口播视频 / 数字人讲解视频"
- 用户有文案，想先转语音、再配数字人画面
- 不要用于：纯图片生成（用 `tongyi-image`）；文生视频/图生视频（用 `seedance-video`）

## 工具 / Tools（细粒度，按序调用）

1. `chanjing_list_audio()` → 选音色 id
2. `chanjing_tts_create({text, voice_id?, speed?})` → `task_id`
3. `chanjing_tts_status({task_id})` → `audio_url` + `duration`
4. `chanjing_list_dp()` → 选数字人 id + 形态 `figure_type/width/height`
5. `chanjing_video_create({audio_url, person_id?, figure_type?, width?, height?})` → `task_id`
6. `chanjing_video_status({task_id})` → `video_url` + `duration`

## 流程 / Pipeline

```
text → chanjing_tts_create → task_id
  → chanjing_tts_status 轮询到 succeeded → audio_url
    → chanjing_video_create(audio_url) → task_id
      → chanjing_video_status 轮询到 completed → video_url
```

## 注意 / Notes

- 视频合成的 `audio` 必须 `{"type": "audio", "wav_url": <TTS 的 audio_url>, "volume": 100}`，明确复用本次 TTS。
- 画布尺寸是顶层 `screen_width/screen_height`（= 数字人形态 width/height），不在 `person` 下，也没有 `canvas` 字段。
- 业务成功以响应体 `code == 0` 为准；失败会带 `trace_id`。
- 音频/视频 URL 非永久存储，交付后提醒用户尽快转存。
- 凭证：`CHANJING_APP_ID` / `CHANJING_SECRET_KEY`（服务端环境变量，.env / .env.local），不要写进代码/对话/日志。
