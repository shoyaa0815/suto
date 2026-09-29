"""Replaceable audio sources and sinks for the Voice interface."""

import asyncio
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


MAX_AUDIO_BYTES = 4 * 1024 * 1024


class AudioUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioClip:
    data: bytes
    format: str = "wav"


class AudioInput(Protocol):
    async def capture(self) -> AudioClip: ...
    async def close(self) -> None: ...


class AudioOutput(Protocol):
    async def play(self, clip: AudioClip) -> None: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...


class FileAudioInput:
    """Read one explicitly configured WAV file per interaction."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def capture(self) -> AudioClip:
        def read() -> bytes:
            if self.path.suffix.casefold() != ".wav":
                raise AudioUnavailable("Audio input must be a WAV file")
            try:
                fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as stream:
                    details = os.fstat(stream.fileno())
                    if not stat.S_ISREG(details.st_mode) or not 0 < details.st_size <= MAX_AUDIO_BYTES:
                        raise AudioUnavailable("Audio input is missing or too large")
                    return stream.read(MAX_AUDIO_BYTES + 1)
            except OSError:
                raise AudioUnavailable("Audio input unavailable") from None

        data = read()
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise AudioUnavailable("Audio input is missing or too large")
        return AudioClip(data)

    async def close(self) -> None:
        pass


class FileAudioOutput:
    """Atomically write generated WAV audio for playback or inspection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def play(self, clip: AudioClip) -> None:
        if clip.format != "wav" or not clip.data or len(clip.data) > MAX_AUDIO_BYTES:
            raise AudioUnavailable("Generated audio is invalid")

        def write() -> None:
            if self.path.suffix.casefold() != ".wav" or self.path.is_symlink():
                raise AudioUnavailable("Audio output must be a regular WAV path")
            temporary = None
            try:
                fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
                temporary = Path(name)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(clip.data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                temporary = None
            except OSError:
                raise AudioUnavailable("Audio output unavailable") from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        write()

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        pass


class AlsaAudioInput:
    def __init__(self, *, device: str | None = None, seconds: int = 10) -> None:
        self.device = device
        self.seconds = seconds
        self._process: asyncio.subprocess.Process | None = None

    async def capture(self) -> AudioClip:
        executable = shutil.which("arecord", path="/usr/bin:/bin")
        if executable is None:
            raise AudioUnavailable("arecord is unavailable")
        args = [executable, "-q", "-d", str(self.seconds), "-f", "S16_LE",
                "-r", "16000", "-c", "1", "-t", "wav"]
        if self.device:
            args += ["-D", self.device]
        args.append("-")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            data, _ = await asyncio.wait_for(self._process.communicate(), self.seconds + 5)
            if self._process.returncode != 0 or not data or len(data) > MAX_AUDIO_BYTES:
                raise AudioUnavailable("Audio capture failed")
            return AudioClip(data)
        except (OSError, asyncio.TimeoutError):
            raise AudioUnavailable("Audio capture failed") from None
        finally:
            await self.close()

    async def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()


class AlsaAudioOutput:
    def __init__(self, *, device: str | None = None) -> None:
        self.device = device
        self._process: asyncio.subprocess.Process | None = None

    async def play(self, clip: AudioClip) -> None:
        if clip.format != "wav" or not clip.data or len(clip.data) > MAX_AUDIO_BYTES:
            raise AudioUnavailable("Generated audio is invalid")
        executable = shutil.which("aplay", path="/usr/bin:/bin")
        if executable is None:
            raise AudioUnavailable("aplay is unavailable")
        args = [executable, "-q"]
        if self.device:
            args += ["-D", self.device]
        args.append("-")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(self._process.communicate(clip.data), 30)
            if self._process.returncode != 0:
                raise AudioUnavailable("Audio playback failed")
        except (OSError, asyncio.TimeoutError):
            raise AudioUnavailable("Audio playback failed") from None
        finally:
            await self.stop()

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        await self.stop()
