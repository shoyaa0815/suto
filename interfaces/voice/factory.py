"""Validated host configuration for the Voice adapter."""

import os
import shutil
from pathlib import Path

from application.configuration import AppSettings

from .audio import AlsaAudioInput, AlsaAudioOutput, FileAudioInput, FileAudioOutput
from .controller import VoiceController
from .providers import OpenAISpeech


class VoiceSetupError(RuntimeError):
    pass


def diagnostics(settings: AppSettings) -> tuple[str, ...]:
    voice = settings.voice
    if voice.input_provider == "alsa":
        input_ready = shutil.which("arecord", path="/usr/bin:/bin") is not None
        input_detail = "arecord installed; device not verified" if input_ready else "arecord missing"
    else:
        source = Path(voice.input_path)
        input_ready = source.is_file() and not source.is_symlink() and source.suffix.casefold() == ".wav"
        input_detail = "WAV file ready" if input_ready else "WAV input file unavailable"
    if voice.output_provider == "alsa":
        output_ready = shutil.which("aplay", path="/usr/bin:/bin") is not None
        output_detail = "aplay installed; device not verified" if output_ready else "aplay missing"
    else:
        target = Path(voice.output_path)
        output_ready = (target.suffix.casefold() == ".wav" and not target.is_symlink()
                        and target.parent.is_dir())
        output_detail = "WAV output path ready" if output_ready else "WAV output path unavailable"
    key_ready = bool(os.environ.get("OPENAI_API_KEY"))
    return (
        f"STT provider: {voice.stt_provider}" +
        (" (OPENAI_API_KEY missing)" if voice.stt_provider == "openai" and not key_ready else ""),
        f"TTS provider: {voice.tts_provider}" +
        (" (OPENAI_API_KEY missing)" if voice.tts_provider == "openai" and not key_ready else ""),
        f"Audio input: {voice.input_provider} ({input_detail})",
        f"Audio output: {voice.output_provider} ({output_detail})",
    )


def build_controller(settings: AppSettings, *, store=None, executor=None) -> VoiceController:
    voice = settings.voice
    checks = diagnostics(settings)
    if voice.stt_provider != "openai" or voice.tts_provider != "openai":
        raise VoiceSetupError("Configure both voice speech providers before starting Voice")
    if not os.environ.get("OPENAI_API_KEY"):
        raise VoiceSetupError("OPENAI_API_KEY is required for the selected speech provider")
    if any("missing" in line or "unavailable" in line for line in checks[2:]):
        raise VoiceSetupError("Configured audio input or output is unavailable")
    speech = OpenAISpeech(os.environ["OPENAI_API_KEY"])
    source = (AlsaAudioInput(device=voice.input_device, seconds=voice.capture_seconds)
              if voice.input_provider == "alsa" else FileAudioInput(voice.input_path))
    sink = (AlsaAudioOutput(device=voice.output_device)
            if voice.output_provider == "alsa" else FileAudioOutput(voice.output_path))
    options = {"store": store}
    if executor is not None:
        options["executor"] = executor
    return VoiceController(source, speech, speech, sink, **options)
