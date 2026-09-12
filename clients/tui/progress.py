from clients.tui.output import write as print


def format_elapsed(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def format_progress(update: dict) -> str:
    elapsed = format_elapsed(update["elapsed_seconds"])
    tokens = f"tokens {update['total_tokens']:,}"
    activity = update["activity"]
    detail = update["detail"]

    if activity == "finished":
        usage = (
            f"input {update['prompt_tokens']:,} / "
            f"output {update['output_tokens']:,}"
        )
        return f"[{elapsed}] ✓ {detail} · {tokens} ({usage})"

    active_for = format_elapsed(update["activity_elapsed_seconds"])
    marker = "…" if update["heartbeat"] else "→"
    return f"[{elapsed}] {marker} {detail} · active {active_for} · {tokens}"


def print_progress(update: dict, prefix: str = "") -> None:
    label = f"[{prefix}] " if prefix else ""
    print(f"{label}{format_progress(update)}")
