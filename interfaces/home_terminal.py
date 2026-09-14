"""Silent placeholder entrypoint for the future home mode."""


def run(mode: str) -> None:
    if mode != "home":
        raise ValueError("the plain home terminal only supports home mode")
