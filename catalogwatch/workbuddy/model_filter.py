"""Which upstream models the text pool may serve.

The WorkBuddy/CodeBuddy account catalog also carries:

- selector pseudo-models (``auto`` / ``default``) that are not real models;
- media-generation models (video, audio/TTS, image) that speak a different
  response protocol than OpenAI Chat;
- non-chat endpoints (embeddings, rerank).

The pool only speaks text chat, so these must never be shown in the console,
counted in the catalog, offered by ``/v1/models``, or picked for ``auto``.
Vision-input chat models (for example ``glm-4.6v``) are **kept**: they still
answer in text.

The decision is keyword-based because the upstream catalog does not expose an
output-modality field. Markers are matched against ``"<model_id> <name>"``.
"""

from __future__ import annotations

PSEUDO_MODEL_IDS = frozenset({"auto", "default", "auto-model", "default-model"})
PSEUDO_MODEL_NAMES = frozenset({"auto", "default", "auto model", "default model"})

# Substrings of a non-text output modality, or of a non-chat endpoint.
NON_TEXT_MARKERS = (
    # Video generation
    "video", "kling", "sora", "veo", "runway", "pika", "seedance", "vidu",
    "cogvideo", "wanx",
    # Audio / speech / music
    "audio", "tts", "asr", "speech", "voice", "whisper", "music", "suno", "song",
    # Image generation / editing
    "image", "dall-e", "dalle", "imagen", "flux", "stable-diffusion", "sdxl",
    "midjourney", "seedream", "kolors",
    # Non-chat endpoints (safety/guard/classification models are listed by some
    # providers but cannot answer a chat completion)
    "embedding", "embed-", "rerank", "moderation", "content-safety", "guard",
    "safety", "reward-model", "classifier",
)


def _text(value: object) -> str:
    return str(value or "").strip().lower()


def is_pseudo_model(model_id: object, name: object = None) -> bool:
    """Whether this entry is a selector placeholder rather than a model."""
    return _text(model_id) in PSEUDO_MODEL_IDS or _text(name) in PSEUDO_MODEL_NAMES


def is_text_chat_model(model_id: object, name: object = None) -> bool:
    """Whether this model can serve an OpenAI Chat completion as text."""
    ident = _text(model_id)
    if not ident or is_pseudo_model(ident, name):
        return False
    haystack = f"{ident} {_text(name)}"
    return not any(marker in haystack for marker in NON_TEXT_MARKERS)
