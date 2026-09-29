"""Conservative text and speech views of a final agent result."""

import re
from dataclasses import dataclass

from workflows.storage.redaction import REDACTED, redact_text


_SENSITIVE = re.compile(
    r"(?i)(?:\b(?:api[ _-]?key|password|secret|token|authorization|bearer|export)\b|"
    r"https?://|[A-Za-z0-9_-]{32,})"
)


@dataclass(frozen=True)
class VoicePresentation:
    display_text: str
    spoken_text: str


def present(text: str, status: str) -> VoicePresentation:
    if status not in {"completed", "waiting_input"}:
        return VoicePresentation("Request did not complete.", "The request could not be completed.")
    display = redact_text(text)
    if not display.strip():
        return VoicePresentation("No response was available.", "No response was available.")
    if (
        display != text or REDACTED in display or _SENSITIVE.search(display)
        or "```" in display or "`" in display
        or len(display) > 280 or len(display.split()) > 50
        or len(display.splitlines()) > 2
    ):
        return VoicePresentation(display, "I have a detailed response. It is available as text.")
    spoken = " ".join(display.split())
    return VoicePresentation(display, spoken)
