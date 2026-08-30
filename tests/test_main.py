import pytest

from main import _parse_args


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["chat", "cli"], ("cli", "chat")),
        (["agent", "cli"], ("cli", "agent")),
        (["chat", "discord"], ("discord", "chat")),
        (["agent", "discord"], ("discord", "agent")),
        (["chat", "line"], ("line", "chat")),
        (["agent", "line"], ("line", "agent")),
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
        ["chat", "unknown"],
        ["chat", "discord", "extra"],
    ],
)
def test_parse_args_rejects_invalid_arguments(args):
    with pytest.raises(SystemExit, match="usage:"):
        _parse_args(args)
