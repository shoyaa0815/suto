"""Non-blocking language selection for the TUI session."""

import asyncio
import threading

from application.language import choose_reply_language


async def choose_reply_language_async(
    prompt: str,
    previous_code: str | None,
):
    """Run Lingua off the UI event loop; its first detection can be expensive."""
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def detect() -> None:
        try:
            choice = choose_reply_language(prompt, previous_code=previous_code)
        except BaseException as error:
            loop.call_soon_threadsafe(deliver, None, error)
        else:
            loop.call_soon_threadsafe(deliver, choice, None)

    def deliver(choice, error: BaseException | None) -> None:
        if result.done():
            return
        if error is not None:
            result.set_exception(error)
        else:
            result.set_result(choice)

    threading.Thread(target=detect, name="suto-language", daemon=True).start()
    return await result
