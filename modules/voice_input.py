"""Phase-3C: voice input - speech-to-text design briefs (optional, offline).

Web dashboard: ``st.audio_input`` records a mic clip; CLI: ``--voice-file``
reads wav/mp3/m4a. Either way the transcript feeds the normal text path
(``input_handler.parse_text``) - voice never bypasses requirement parsing.

Engine: faster-whisper (CTranslate2, no torch, CPU int8). Optional exactly
like FEA: missing library / ``VOICE_ENABLED=0`` raises a clear
:class:`VoiceInputError` instead of crashing, and the typed-text path keeps
working. Model weights download once into ``cfg.VOICE_MODEL_DIR`` (default
``output/voice``, gitignored) - copy the files there by hand on machines
that never go online.

Honest limits: short spoken briefs only (not dictation); the default
``base.en`` model handles accents and numbers well on a modern CPU but
quality drops on noisy audio or heavy code-switching - the parsed
requirements block always shows exactly what was understood.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import config as cfg


class VoiceInputError(RuntimeError):
    """User-facing voice failure (disabled, missing engine, bad audio...)."""


# size -> cached WhisperModel (construction downloads on first use)
_models: dict[str, Any] = {}


def available() -> bool:
    """True when the faster-whisper engine is importable."""
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def _load_model(size: str) -> Any:
    """Cached CPU int8 model; construction failures -> VoiceInputError."""
    cached = _models.get(size)
    if cached is not None:
        return cached
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise VoiceInputError(
            "voice input needs faster-whisper (pip install faster-whisper)"
        ) from exc
    root = cfg.VOICE_MODEL_DIR
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        model = WhisperModel(size, device="cpu", compute_type="int8",
                             download_root=str(root))
    except Exception as exc:
        raise VoiceInputError(
            f"whisper model '{size}' unavailable ({type(exc).__name__}) - "
            f"connect once to download it, or copy the model files into "
            f"{root}"
        ) from exc
    _models[size] = model
    return model


def reset_models() -> None:
    """Drop cached models (tests, model-size switches)."""
    _models.clear()


def transcribe(audio: bytes | bytearray | memoryview | str | Path,
               *, model_size: str | None = None,
               language: str | None = None) -> str:
    """Speech -> text.

    ``audio`` is raw clip bytes (web app) or a filesystem path (CLI).
    ``model_size``/``language`` default to ``VOICE_MODEL``/``VOICE_LANGUAGE``.
    Raises :class:`VoiceInputError` with a user-facing message on any
    voice-side failure - callers treat it as recoverable.
    """
    if not cfg.VOICE_ENABLED:
        raise VoiceInputError("voice input disabled (VOICE_ENABLED=0)")
    if not available():
        raise VoiceInputError(
            "voice input needs faster-whisper (pip install faster-whisper)")
    size = model_size or cfg.VOICE_MODEL
    lang = cfg.VOICE_LANGUAGE if language is None else language

    source: Any
    if isinstance(audio, (bytes, bytearray, memoryview)):
        raw = bytes(audio)
        if not raw:
            raise VoiceInputError("empty audio clip - record it again")
        source = io.BytesIO(raw)
    else:
        path = Path(audio)
        if not path.is_file():
            raise VoiceInputError(f"audio file not found: {path}")
        source = str(path)

    model = _load_model(size)
    try:
        segments, _info = model.transcribe(source, language=lang,
                                           vad_filter=True, beam_size=5)
        parts = [s.text.strip() for s in segments]
    except VoiceInputError:
        raise
    except Exception as exc:
        raise VoiceInputError(
            f"could not transcribe audio ({type(exc).__name__}: {exc}) - "
            "use wav/mp3/m4a recorded at normal volume"
        ) from exc
    text = " ".join(p for p in parts if p).strip()
    if not text:
        raise VoiceInputError("no speech detected in the clip")
    return text


def status() -> dict:
    """Status block for UI/CLI diagnostics."""
    return {
        "enabled": bool(cfg.VOICE_ENABLED),
        "available": bool(cfg.VOICE_ENABLED) and available(),
        "engine": "faster-whisper" if available() else None,
        "model": cfg.VOICE_MODEL,
        "model_dir": str(cfg.VOICE_MODEL_DIR),
    }
