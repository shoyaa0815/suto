import re
import unicodedata
from dataclasses import dataclass

from lingua import Language, LanguageDetectorBuilder


DEFAULT_LANGUAGE_CODE = "th"

# A small distance rejects ambiguous inputs while still allowing normal chat
# messages. Empty, emoji-only, and very short Latin input use the fallback.
_DETECTOR = (
    LanguageDetectorBuilder.from_all_spoken_languages()
    .with_minimum_relative_distance(0.02)
    .build()
)

_LOCAL_ALIASES = {
    "ไทย": Language.THAI,
    "อังกฤษ": Language.ENGLISH,
    "จีน": Language.CHINESE,
    "ญี่ปุ่น": Language.JAPANESE,
    "เกาหลี": Language.KOREAN,
    "ฝรั่งเศส": Language.FRENCH,
    "เยอรมัน": Language.GERMAN,
    "สเปน": Language.SPANISH,
    "อิตาลี": Language.ITALIAN,
    "โปรตุเกส": Language.PORTUGUESE,
    "รัสเซีย": Language.RUSSIAN,
    "เวียดนาม": Language.VIETNAMESE,
}


@dataclass(frozen=True)
class ReplyLanguage:
    code: str
    name: str
    source: str


def _language_code(language: Language) -> str:
    return language.iso_code_639_1.name.lower()


def _language_aliases() -> dict[str, Language]:
    aliases = dict(_LOCAL_ALIASES)
    for language in Language.all():
        aliases[language.name.lower().replace("_", " ")] = language
        aliases[_language_code(language)] = language
    return aliases


_ALIASES = _language_aliases()


def _explicit_language(text: str) -> Language | None:
    lowered = text.casefold()
    for alias in sorted(_ALIASES, key=len, reverse=True):
        escaped = re.escape(alias)
        boundary = (
            r"(?![A-Za-z])"
            if any("THAI" in unicodedata.name(char, "") for char in alias)
            else r"\b"
        )
        patterns = (
            rf"\b(?:answer|reply|respond|write)\b[^\n.!?]{{0,20}}"
            rf"\b(?:in|using)\s+{escaped}{boundary}",
            rf"(?:ตอบ|เขียน|สรุป|แปล)(?:กลับ)?[^\n.!?]{{0,60}}?"
            rf"(?:เป็น|ด้วย)\s*(?:ภาษา)?\s*{escaped}{boundary}",
            rf"(?:ตอบ|เขียน|สรุป|แปล)(?:กลับ)?\s*"
            rf"(?:ภาษา)?\s*{escaped}{boundary}",
        )
        if any(re.search(pattern, lowered) for pattern in patterns):
            return _ALIASES[alias]
    return None


def _from_code(code: str | None) -> Language | None:
    if not code:
        return None
    normalized = code.strip().lower()
    return next(
        (
            language
            for language in Language.all()
            if _language_code(language) == normalized
        ),
        None,
    )


def choose_reply_language(
    original_prompt: str,
    previous_code: str | None = None,
) -> ReplyLanguage:
    """Choose explicit request, detected prompt language, then prior/default."""
    explicit = _explicit_language(original_prompt)
    if explicit is not None:
        return ReplyLanguage(
            code=_language_code(explicit),
            name=explicit.name.title(),
            source="explicit",
        )

    letters = [char for char in original_prompt if char.isalpha()]
    is_short_latin = len(letters) < 10 and all(
        "LATIN" in unicodedata.name(char, "") for char in letters
    )
    detected = (
        None
        if len(letters) < 3 or is_short_latin
        else _DETECTOR.detect_language_of(original_prompt)
    )
    if detected is not None:
        return ReplyLanguage(
            code=_language_code(detected),
            name=detected.name.title(),
            source="detected",
        )

    previous = _from_code(previous_code)
    fallback = previous or Language.THAI
    return ReplyLanguage(
        code=_language_code(fallback),
        name=fallback.name.title(),
        source="previous" if previous is not None else "default",
    )


def detect_language_code(text: str) -> str | None:
    """Detect output language without applying conversation fallbacks."""
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 3:
        return None

    detected = _DETECTOR.detect_language_of(text)
    return _language_code(detected) if detected is not None else None
