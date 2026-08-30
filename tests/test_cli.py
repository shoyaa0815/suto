from clients.cli.bot import _print_help
from progress import format_elapsed, print_progress


def test_format_elapsed_uses_minutes_and_seconds():
    assert format_elapsed(125) == "02:05"


def test_print_help_lists_exit_commands(capsys):
    _print_help()

    output = capsys.readouterr().out
    assert "/help" in output
    assert "/exit" in output
    assert "/quit" in output


def test_print_progress_shows_activity_time_and_tokens(capsys):
    print_progress(
        {
            "activity": "tool",
            "detail": "running search_web (query=latest news)",
            "elapsed_seconds": 65,
            "activity_elapsed_seconds": 12,
            "prompt_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "heartbeat": True,
        }
    )

    output = capsys.readouterr().out
    assert "[01:05]" in output
    assert "active 00:12" in output
    assert "tokens 120" in output
    assert "search_web" in output


def test_print_progress_can_label_a_discord_request(capsys):
    print_progress(
        {
            "activity": "finished",
            "detail": "completed",
            "elapsed_seconds": 5,
            "activity_elapsed_seconds": 0,
            "prompt_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 100,
            "heartbeat": False,
        },
        prefix="discord:123:456",
    )

    assert capsys.readouterr().out.startswith("[discord:123:456] [00:05]")
