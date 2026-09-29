"""Text-to-speech boundary; callers pass only presentation-policy text."""

from typing import Protocol

from .audio import AudioClip


class TextToSpeech(Protocol):
    async def synthesize(self, text: str) -> AudioClip: ...
    async def close(self) -> None: ...
