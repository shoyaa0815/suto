"""Parse explicit CLI reminder commands without asking a model to choose a tool."""

import re
from dataclasses import dataclass


_THAI_COUNTS = {
    "หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5,
    "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9, "สิบ": 10,
}
_COUNT = "|".join((r"\d+", *sorted(_THAI_COUNTS, key=len, reverse=True)))
_RELATIVE = re.compile(rf"^อีก\s*({_COUNT})\s*(นาที|ชั่วโมง)\s*(.+)$")
_CLOCK = re.compile(r"^(\d{1,2})[:.](\d{2})\s+(.+)$")


@dataclass(frozen=True)
class ReminderInput:
    title: str
    minutes: int | None = None
    clock_time: str | None = None


def parse_reminder_input(argument: str) -> ReminderInput:
    """Accept only explicit relative durations or next-occurrence clock times."""
    text = argument.strip()
    relative = _RELATIVE.fullmatch(text)
    if relative:
        amount, unit, title = relative.groups()
        count = int(amount) if amount.isdecimal() else _THAI_COUNTS[amount]
        minutes = count * (60 if unit == "ชั่วโมง" else 1)
        if not 1 <= minutes <= 525_600:
            raise ValueError("ระยะเวลาต้องอยู่ระหว่าง 1 ถึง 525600 นาที")
        return ReminderInput(_title(title), minutes=minutes)
    clock = _CLOCK.fullmatch(text)
    if clock:
        hour, minute, title = clock.groups()
        hour_number, minute_number = int(hour), int(minute)
        if hour_number > 23 or minute_number > 59:
            raise ValueError("เวลาไม่ถูกต้อง ใช้เวลาแบบ HH:MM")
        return ReminderInput(
            _title(title), clock_time=f"{hour_number:02d}:{minute_number:02d}"
        )
    raise ValueError("ใช้ /reminder อีก 5 นาที เตือนกินข้าว หรือ /reminder 00:05 เตือนนอน")


def _title(text: str) -> str:
    title = re.sub(r"^เตือน(?:ให้)?\s*", "", text).strip()
    if not title:
        raise ValueError("กรุณาระบุสิ่งที่ต้องการให้เตือน")
    return title
