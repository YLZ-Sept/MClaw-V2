from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PURE_TEXT_FILES = [
    "plugins/word-maker/word_brain_helper.py",
    "plugins/excel-maker/plugin.py",
    "plugins/ppt-maker/ppt_brain_adapter.py",
    "plugins/finance-auto/finance_auto_backend/ai/router.py",
    "plugins/fin-pulse/plugin.py",
    "plugins/fin-pulse/finpulse_ai/filter.py",
    "plugins/fin-pulse/finpulse_ai/rules_suggest.py",
    "plugins/media-strategy/media_ai/analyzer.py",
    "plugins/idea-research/idea_pipeline.py",
    "plugins/clip-sense/clip_asr_client.py",
    "plugins/subtitle-craft/subtitle_asr_client.py",
    "plugins/media-post/mediapost_pipeline.py",
    "plugins/ecommerce-image/ecom_execution.py",
    "plugins/ecommerce-image/ecom_prompt_optimizer.py",
    "plugins/tongyi-image/tongyi_prompt_optimizer.py",
    "plugins/seedance-video/prompt_optimizer.py",
    "plugins/seedance-video/long_video.py",
    "plugins/happyhorse-video/happyhorse_prompt_optimizer.py",
    "plugins/happyhorse-video/happyhorse_long_video.py",
    "plugins/manga-studio/script_writer.py",
]

AFFECTED_PLUGINS = [
    "word-maker",
    "excel-maker",
    "ppt-maker",
    "finance-auto",
    "fin-pulse",
    "media-strategy",
    "idea-research",
    "clip-sense",
    "subtitle-craft",
    "media-post",
    "ecommerce-image",
    "tongyi-image",
    "seedance-video",
    "happyhorse-video",
    "manga-studio",
]


def test_pure_text_paths_do_not_call_brain_or_vendor_chat_surfaces() -> None:
    forbidden = (
        ".get_brain(",
        ".think_lightweight(",
        ".think(",
        ".chat_completion(",
        ".qwen_plus_call(",
        "messages_create_async(",
        "compatible-mode/v1/chat/completions",
    )
    violations: list[str] = []
    for relative in PURE_TEXT_FILES:
        text = (ROOT / relative).read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                violations.append(f"{relative}: {token}")
    assert violations == []


def test_all_migrated_plugin_dist_pages_ship_the_model_selector() -> None:
    canonical = (
        ROOT / "plugins/word-maker/ui/dist/_assets.mclaw-llm-selector.js"
    ).read_bytes()
    selector_text = canonical.decode("utf-8")
    assert "严格锁定，不会静默回退" in selector_text
    assert "已失效，请重新选择" in selector_text
    for plugin_id in AFFECTED_PLUGINS:
        dist = ROOT / "plugins" / plugin_id / "ui" / "dist"
        assert (dist / "_assets.mclaw-llm-selector.js").read_bytes() == canonical
        html = (dist / "index.html").read_text(encoding="utf-8")
        assert html.count(.mclaw-llm-selector.js") == 1
        assert f'data-plugin-id="{plugin_id}"' in html

    clip_html = (ROOT / "plugins/clip-sense/ui/dist/index.html").read_text(encoding="utf-8")
    subtitle_html = (ROOT / "plugins/subtitle-craft/ui/dist/index.html").read_text(
        encoding="utf-8"
    )
    assert "dashscope_analysis_api_key" not in clip_html
    assert '<option value="qwen-' not in subtitle_html
