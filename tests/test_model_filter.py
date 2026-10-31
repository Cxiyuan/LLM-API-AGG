"""模型过滤：池只服务文本对话，伪模型与媒体/非对话模型必须被剔除。"""

from __future__ import annotations

from catalogwatch.workbuddy.model_filter import (
    is_pseudo_model,
    is_text_chat_model,
    is_tool_capable_model,
)


def test_pseudo_models_are_not_models():
    for ident in ("auto", "default", "AUTO", " default "):
        assert is_pseudo_model(ident)
        assert not is_text_chat_model(ident, ident)
    assert is_pseudo_model("whatever", "Default")
    # 只是名字里带 auto 的真实模型不能误伤
    assert is_text_chat_model("auto-think", "Auto Think")


def test_media_and_non_chat_models_are_filtered():
    cases = [
        ("kling-video-v3", "Kling-Video-V3"),
        ("minimax-video-h3", "MiniMax-H3"),
        ("hunyuan-image-alpha-edit", "Hunyuan Image Alpha Edit"),
        ("gpt-4o-audio-preview", ""),
        ("qwen-tts", ""),
        ("whisper-1", ""),
        ("suno-v4", ""),
        ("text-embedding-3-large", ""),
        ("bge-rerank", ""),
    ]
    for model_id, name in cases:
        assert not is_text_chat_model(model_id, name), model_id


def test_diffusion_models_are_text_but_not_tool_safe():
    model_id = "google/diffusiongemma-26b-a4b-it"
    assert is_text_chat_model(model_id, "DiffusionGemma")
    assert not is_tool_capable_model(model_id, "DiffusionGemma")
    assert is_tool_capable_model("z-ai/glm-5.3-flash", "GLM-5.3-Flash")


def test_text_chat_models_survive():
    cases = [
        ("deepseek-v4-flash", "Deepseek-V4-Flash"),
        # 视觉输入、文本输出：仍然是文本对话模型
        ("glm-4.6v", "GLM-4.6V"),
        ("hunyuan-chat", "Hunyuan-Turbos"),
        ("kimi-k3-1", "Kimi-K3"),
        ("hy3", "Hy3"),
        ("step-5-preview", "Step-5-Preview"),
        ("minimax-m3.1-flash", "MiniMax-M3.1-Flash"),
    ]
    for model_id, name in cases:
        assert is_text_chat_model(model_id, name), model_id
