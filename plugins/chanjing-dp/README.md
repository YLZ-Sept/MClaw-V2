# 蝉镜数字人 / Chanjing Digital Human

通过蝉镜 AI 开放平台生成语音（TTS）与数字人口播视频的 Mclaw 插件。

## 能力

- 文本 → 语音（TTS）
- 语音 → 数字人口播视频

## 凭证

服务端环境变量 `CHANJING_APP_ID` / `CHANJING_SECRET_KEY`（.env / .env.local，均已 .gitignore）。

`secret_key` 与 `access_token` 永不出现在日志 / 返回值中。

## AI 工具

细粒度 4+2 个工具（见 `SKILL.md`）：
`chanjing_list_audio`、`chanjing_list_dp`、`chanjing_tts_create`、`chanjing_tts_status`、`chanjing_video_create`、`chanjing_video_status`

## 开发

复用 `mclaw.integrations.chanjing.ChanjingClient`（token 缓存 + `code==0` 判定 + 轮询退避）。
