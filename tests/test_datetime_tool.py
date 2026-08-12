from datetime import datetime

from harness import datetime_tool


class _FixedDateTime(datetime):
    """datetime.now().astimezone() pinned to a known instant.

    astimezone() is overridden to return self instead of converting to the
    system's local timezone, so results don't depend on where the tests run.
    """

    def astimezone(self, tz=None):
        return self

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 5, 9, 7)


def _fields(result: str) -> dict[str, str]:
    return dict(line.split(": ", 1) for line in result.split("\n"))


def test_returns_all_expected_fields_in_order(monkeypatch):
    monkeypatch.setattr(datetime_tool, "datetime", _FixedDateTime)

    result = datetime_tool.get_current_datetime()
    labels = [line.split(":", 1)[0] for line in result.split("\n")]

    assert labels == [
        "day_of_week",
        "day_of_month",
        "month",
        "year",
        "buddhist_year",
        "date_iso",
        "time_24h",
    ]


def test_buddhist_year_is_gregorian_year_plus_543(monkeypatch):
    monkeypatch.setattr(datetime_tool, "datetime", _FixedDateTime)

    fields = _fields(datetime_tool.get_current_datetime())

    assert fields["year"] == "2026"
    assert fields["buddhist_year"] == "2569"


def test_date_iso_matches_the_other_date_fields(monkeypatch):
    monkeypatch.setattr(datetime_tool, "datetime", _FixedDateTime)

    fields = _fields(datetime_tool.get_current_datetime())

    assert fields["date_iso"] == "2026-01-05"


def test_day_of_month_is_not_zero_padded(monkeypatch):
    # day_of_month uses now.day (an int) rather than strftime("%d"), which
    # would zero-pad. A single-digit day is the case that would catch a
    # regression back to the padded form.
    monkeypatch.setattr(datetime_tool, "datetime", _FixedDateTime)

    fields = _fields(datetime_tool.get_current_datetime())

    assert fields["day_of_month"] == "5"


def test_month_and_weekday_are_full_english_names(monkeypatch):
    monkeypatch.setattr(datetime_tool, "datetime", _FixedDateTime)

    fields = _fields(datetime_tool.get_current_datetime())

    assert fields["month"] == "January"
    assert fields["day_of_week"] == "Monday"


def test_time_24h_is_zero_padded_hour_and_minute(monkeypatch):
    class _EarlyMorning(_FixedDateTime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 5, 9, 7)

    monkeypatch.setattr(datetime_tool, "datetime", _EarlyMorning)

    fields = _fields(datetime_tool.get_current_datetime())

    assert fields["time_24h"] == "09:07"


def test_schema_name_matches_the_registered_tool_name():
    assert datetime_tool.SCHEMA["function"]["name"] == "get_current_datetime"
