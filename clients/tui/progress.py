from clients.tui.output import write_styled


MAX_PROGRESS_DETAIL_CHARS = 60


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def format_progress(update: dict) -> str:
    activity = update["activity"]
    detail = " ".join(str(update["detail"]).split())
    if len(detail) > MAX_PROGRESS_DETAIL_CHARS:
        detail = detail[: MAX_PROGRESS_DETAIL_CHARS - 3].rstrip() + "..."

    if activity == "finished":
        return f"{detail} · {update['total_tokens']:,} tokens"

    return detail


def progress_style(update: dict) -> str:
    activity = update["activity"]
    detail = str(update["detail"]).casefold()
    if activity == "tool":
        return "#60a5fa"
    if any(status in detail for status in (": failed", ": timed out", ": blocked")):
        return "#f87171"
    if activity == "finished" and detail != "completed":
        return "#fbbf24"
    return "#4ade80"


def print_progress(update: dict, prefix: str = "") -> None:
    # Keep the terminal focused on tool usage and the final result. Heartbeats
    # and internal model stages remain available to other clients.
    if update.get("heartbeat") or update["activity"] not in {
        "tool",
        "tool_done",
        "finished",
    }:
        return
    label = f"[{prefix}] " if prefix else ""
    write_styled(
        f"{label}{format_progress(update)}",
        progress_style(update),
    )
