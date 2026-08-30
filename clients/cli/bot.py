import asyncio

from ai import ask_local_ai
from language import choose_reply_language
from modes import get_mode_policy
from progress import print_progress

EXIT_COMMANDS = frozenset({"/exit", "/quit"})


def _print_help() -> None:
    print("Commands:")
    print("  /help  show available commands")
    print("  /exit  exit suto")
    print("  /quit  exit suto")


async def _chat_loop(mode: str) -> None:
    previous_language_code: str | None = None
    print(f"suto CLI (mode: {mode}) — type /help for commands")

    while True:
        try:
            prompt = input("> ").strip()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print("\nbye")
            return

        if not prompt:
            continue
        if prompt.lower() == "/help":
            _print_help()
            continue
        if prompt.lower() in EXIT_COMMANDS:
            print("bye")
            return

        reply_language = choose_reply_language(
            prompt,
            previous_code=previous_language_code,
        )
        previous_language_code = reply_language.code

        try:
            answer = await ask_local_ai(
                prompt,
                mode=mode,
                reply_language=reply_language,
                progress_callback=print_progress,
            )
        except KeyboardInterrupt:
            print("\nrequest cancelled")
            continue

        print(f"suto> {answer}")


def run(mode: str) -> None:
    # Keep direct use of this client subject to the same validation as main.py.
    get_mode_policy(mode)
    asyncio.run(_chat_loop(mode))
