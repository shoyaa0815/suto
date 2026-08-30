import pytest

from main import _parse_args


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["personal", "discord"], ("discord", "personal")),
        (["private", "discord"], ("discord", "private")),
        (["personal", "line"], ("line", "personal")),
        (["private", "line"], ("line", "private")),
    ],
)
def test_parse_args_accepts_client_and_mode(args, expected):
    assert _parse_args(args) == expected


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["discord"],
        ["secret", "discord"],
        ["personal", "unknown"],
        ["personal", "discord", "extra"],
    ],
)
def test_parse_args_rejects_invalid_arguments(args):
    with pytest.raises(SystemExit, match="usage:"):
        _parse_args(args)
