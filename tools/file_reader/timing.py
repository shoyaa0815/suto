import time


def log_timing(event: str, started: float, **fields: object) -> None:
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"[timing] event={event} ms={elapsed_ms} {details}".rstrip())
