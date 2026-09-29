"""Speech-to-text boundary; transcriptions are untrusted user text."""

from typing import Protocol

from .audio import AudioClip


class SpeechToText(Protocol):
    async def transcribe(self, audio: AudioClip) -> str: ...
    async def close(self) -> None: ...
