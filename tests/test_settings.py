import pytest

from core.settings import env_float, env_int, env_text


def test_environment_settings_use_defaults(monkeypatch):
    monkeypatch.delenv("SUTO_TEST_TEXT", raising=False)
    monkeypatch.delenv("SUTO_TEST_INT", raising=False)
    monkeypatch.delenv("SUTO_TEST_FLOAT", raising=False)

    assert env_text("SUTO_TEST_TEXT", "default") == "default"
    assert env_int("SUTO_TEST_INT", 10, minimum=1) == 10
    assert env_float("SUTO_TEST_FLOAT", 0.2, minimum=0) == 0.2


def test_environment_settings_parse_values(monkeypatch):
    monkeypatch.setenv("SUTO_TEST_TEXT", " custom-model ")
    monkeypatch.setenv("SUTO_TEST_INT", "25")
    monkeypatch.setenv("SUTO_TEST_FLOAT", "0.5")

    assert env_text("SUTO_TEST_TEXT", "default") == "custom-model"
    assert env_int("SUTO_TEST_INT", 10, minimum=1) == 25
    assert env_float("SUTO_TEST_FLOAT", 0.2, minimum=0, maximum=1) == 0.5


@pytest.mark.parametrize("value", ["nope", "1.5"])
def test_integer_setting_rejects_invalid_value(monkeypatch, value):
    monkeypatch.setenv("SUTO_TEST_INT", value)

    with pytest.raises(ValueError, match="SUTO_TEST_INT must be an integer"):
        env_int("SUTO_TEST_INT", 10)


def test_environment_setting_rejects_out_of_range_value(monkeypatch):
    monkeypatch.setenv("SUTO_TEST_LIMIT", "0")

    with pytest.raises(ValueError, match="must be at least 1"):
        env_int("SUTO_TEST_LIMIT", 10, minimum=1)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_float_setting_rejects_non_finite_value(monkeypatch, value):
    monkeypatch.setenv("SUTO_TEST_FLOAT", value)

    with pytest.raises(ValueError, match="must be a finite number"):
        env_float("SUTO_TEST_FLOAT", 0.2)
