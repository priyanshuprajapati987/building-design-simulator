"""Phase-3C: voice input - engine paths, config, CLI wiring (hermetic).

No model download, no network, no microphone: the Whisper engine is faked
via ``_load_model`` / ``sys.modules`` injection, transcripts are asserted
through the same ``parse_text`` door the typed brief uses.
"""
from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import pytest

import config as cfg
from modules import voice_input


class _Seg:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    """Duck-typed WhisperModel: '|' separates segments, ``fail`` raises."""

    def __init__(self, text: str = "Design a 5 floor office in Mumbai",
                 fail: Exception | None = None) -> None:
        self._text = text
        self._fail = fail
        self.calls: list[tuple[object, dict]] = []

    def transcribe(self, audio, **kw):
        self.calls.append((audio, kw))
        if self._fail is not None:
            raise self._fail
        return [_Seg(t) for t in self._text.split("|")], None


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    voice_input.reset_models()
    monkeypatch.setattr(cfg, "VOICE_ENABLED", True)
    yield
    voice_input.reset_models()


# ---------------------------------------------------------------------------
# availability / config
# ---------------------------------------------------------------------------

def test_available_true_with_engine_installed():
    # faster-whisper is in the venv (requirements.txt Phase-3C entry)
    assert voice_input.available() is True


def test_available_false_when_engine_missing(monkeypatch):
    import builtins

    real = builtins.__import__

    def _fake(name, *a, **k):
        if name == "faster_whisper":
            raise ImportError("no engine")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _fake)
    assert voice_input.available() is False


def test_config_defaults():
    assert isinstance(cfg.VOICE_MODEL, str) and cfg.VOICE_MODEL
    assert isinstance(cfg.VOICE_MODEL_DIR, Path)
    assert str(cfg.VOICE_MODEL_DIR)
    assert cfg.VOICE_LANGUAGE is None or isinstance(cfg.VOICE_LANGUAGE, str)


def test_status_shape():
    st = voice_input.status()
    assert st["enabled"] is True
    assert st["available"] is True
    assert st["engine"] == "faster-whisper"
    assert st["model"] == cfg.VOICE_MODEL


# ---------------------------------------------------------------------------
# transcribe() error paths
# ---------------------------------------------------------------------------

def test_disabled_raises(monkeypatch):
    monkeypatch.setattr(cfg, "VOICE_ENABLED", False)
    with pytest.raises(voice_input.VoiceInputError, match="VOICE_ENABLED"):
        voice_input.transcribe(b"RIFFdata")


def test_missing_engine_raises(monkeypatch):
    monkeypatch.setattr(voice_input, "available", lambda: False)
    with pytest.raises(voice_input.VoiceInputError, match="faster-whisper"):
        voice_input.transcribe(b"RIFFdata")


def test_empty_clip_raises():
    with pytest.raises(voice_input.VoiceInputError, match="empty"):
        voice_input.transcribe(b"")


def test_missing_file_raises(tmp_path):
    with pytest.raises(voice_input.VoiceInputError, match="not found"):
        voice_input.transcribe(tmp_path / "nope.wav")


def test_decode_failure_wrapped(monkeypatch):
    fake = _FakeModel(fail=RuntimeError("boom"))
    monkeypatch.setattr(voice_input, "_load_model", lambda size: fake)
    with pytest.raises(voice_input.VoiceInputError,
                       match="could not transcribe"):
        voice_input.transcribe(b"RIFFnotaudio")


def test_no_speech_raises(monkeypatch):
    fake = _FakeModel("")
    monkeypatch.setattr(voice_input, "_load_model", lambda size: fake)
    with pytest.raises(voice_input.VoiceInputError, match="no speech"):
        voice_input.transcribe(b"RIFFsilence")


# ---------------------------------------------------------------------------
# transcribe() happy paths
# ---------------------------------------------------------------------------

def test_bytes_clip_joins_segments(monkeypatch):
    fake = _FakeModel("Design a|5 floor office in Mumbai")
    monkeypatch.setattr(voice_input, "_load_model", lambda size: fake)
    txt = voice_input.transcribe(b"RIFFdata", language="en")
    assert txt == "Design a 5 floor office in Mumbai"
    audio_arg, kw = fake.calls[0]
    assert isinstance(audio_arg, io.BytesIO)
    assert kw["language"] == "en"
    assert kw["vad_filter"] is True


def test_path_file_forwarded(monkeypatch, tmp_path):
    wav = tmp_path / "brief.wav"
    wav.write_bytes(b"RIFFdata")
    fake = _FakeModel("3 floor house in Pune")
    monkeypatch.setattr(voice_input, "_load_model", lambda size: fake)
    txt = voice_input.transcribe(wav)
    assert txt == "3 floor house in Pune"
    audio_arg, _ = fake.calls[0]
    assert audio_arg == str(wav)


def test_transcript_feeds_text_parser(monkeypatch):
    # the whole point: voice -> parse_text is the single front door
    from modules import input_handler, requirement_analyzer

    fake = _FakeModel("5 floor office in Mumbai")
    monkeypatch.setattr(voice_input, "_load_model", lambda size: fake)
    txt = voice_input.transcribe(b"RIFFdata")
    req = requirement_analyzer.analyze(input_handler.load(text=txt))
    assert req.building_type == "office"
    assert req.floors == 5
    assert req.city == "Mumbai"


# ---------------------------------------------------------------------------
# model cache
# ---------------------------------------------------------------------------

def test_model_cache_and_reset(monkeypatch):
    made: list[dict] = []

    class _WhisperModel:
        def __init__(self, size, **kw):
            made.append({"size": size, **kw})

    fake_module = types.SimpleNamespace(WhisperModel=_WhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    voice_input.reset_models()
    m1 = voice_input._load_model("base.en")
    m2 = voice_input._load_model("base.en")
    assert m1 is m2
    assert len(made) == 1
    assert made[0]["size"] == "base.en"
    assert made[0]["device"] == "cpu"
    assert made[0]["compute_type"] == "int8"
    assert made[0]["download_root"] == str(cfg.VOICE_MODEL_DIR)

    voice_input.reset_models()
    voice_input._load_model("tiny.en")
    assert len(made) == 2
    assert made[1]["size"] == "tiny.en"


# ---------------------------------------------------------------------------
# CLI wiring (main.py --voice-file)
# ---------------------------------------------------------------------------

def test_cli_voice_file_runs_pipeline(tmp_path, monkeypatch):
    from main import main

    wav = tmp_path / "brief.wav"
    wav.write_bytes(b"RIFFdata")
    seen: dict = {}

    def _fake_run(**kw):
        seen.update(kw)
        return {"requirements": {"raw_text": kw.get("text")},
                "results": [], "winner": {}, "files": {}}

    monkeypatch.setattr("modules.pipeline.run", _fake_run)
    monkeypatch.setattr(voice_input, "transcribe",
                        lambda *a, **k: "3 floor house in Pune")
    rc = main(["--voice-file", str(wav), "--json",
               "--no-pdf", "--no-images", "--no-ifc"])
    assert rc == 0
    assert seen["text"] == "3 floor house in Pune"
    assert seen["data"] is None


def test_cli_voice_file_error_returns_2(tmp_path, monkeypatch, capsys):
    from main import main

    wav = tmp_path / "brief.wav"
    wav.write_bytes(b"RIFFdata")

    def _boom(*a, **k):
        raise voice_input.VoiceInputError("model 'base.en' unavailable")

    monkeypatch.setattr(voice_input, "transcribe", _boom)
    rc = main(["--voice-file", str(wav), "--json"])
    assert rc == 2
    assert "ERROR: model 'base.en' unavailable" in capsys.readouterr().err


def test_cli_typed_text_wins_over_voice_file(tmp_path, monkeypatch):
    from main import main

    wav = tmp_path / "brief.wav"
    wav.write_bytes(b"RIFFdata")
    seen: dict = {}

    def _fake_run(**kw):
        seen.update(kw)
        return {"requirements": {"raw_text": kw.get("text")},
                "results": [], "winner": {}, "files": {}}

    monkeypatch.setattr("modules.pipeline.run", _fake_run)

    def _never(*a, **k):
        raise AssertionError("voice must not run when text is given")

    monkeypatch.setattr(voice_input, "transcribe", _never)
    rc = main(["typed brief", "--voice-file", str(wav), "--json",
               "--no-pdf", "--no-images", "--no-ifc"])
    assert rc == 0
    assert seen["text"] == "typed brief"
