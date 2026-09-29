"""One optional speech provider over the existing HTTP dependency."""

import asyncio
import json
from dataclasses import dataclass, field

import aiohttp

from .audio import AudioClip, MAX_AUDIO_BYTES


API_BASE = "https://api.openai.com/v1"
STT_MODEL = "gpt-4o-mini-transcribe"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"


class SpeechUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenAISpeech:
    """Explicit opt-in provider. The API key never enters config or diagnostics."""

    api_key: str = field(repr=False)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def transcribe(self, audio: AudioClip) -> str:
        if audio.format != "wav" or not 0 < len(audio.data) <= MAX_AUDIO_BYTES:
            raise SpeechUnavailable("Audio transcription unavailable")
        form = aiohttp.FormData()
        form.add_field("file", audio.data, filename="capture.wav", content_type="audio/wav")
        form.add_field("model", STT_MODEL)
        form.add_field("response_format", "json")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.post(f"{API_BASE}/audio/transcriptions", data=form,
                                        headers=self._headers(), allow_redirects=False) as response:
                    if response.status != 200:
                        raise SpeechUnavailable("Audio transcription unavailable")
                    raw = await response.content.read(16_385)
            if len(raw) > 16_384:
                raise SpeechUnavailable("Audio transcription unavailable")
            payload = json.loads(raw)
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or len(text) > 8000:
                raise SpeechUnavailable("Audio transcription unavailable")
            return text
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            raise SpeechUnavailable("Audio transcription unavailable") from None

    async def synthesize(self, text: str) -> AudioClip:
        if not isinstance(text, str) or not 0 < len(text) <= 4096:
            raise SpeechUnavailable("Speech synthesis unavailable")
        body = {"model": TTS_MODEL, "voice": TTS_VOICE, "input": text,
                "response_format": "wav"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.post(f"{API_BASE}/audio/speech", json=body,
                                        headers=self._headers(), allow_redirects=False) as response:
                    if response.status != 200:
                        raise SpeechUnavailable("Speech synthesis unavailable")
                    data = await response.content.read(MAX_AUDIO_BYTES + 1)
            if not 0 < len(data) <= MAX_AUDIO_BYTES or not data.startswith(b"RIFF") or data[8:12] != b"WAVE":
                raise SpeechUnavailable("Speech synthesis unavailable")
            return AudioClip(data)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            raise SpeechUnavailable("Speech synthesis unavailable") from None

    async def close(self) -> None:
        pass
