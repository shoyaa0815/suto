import sys

from dotenv import load_dotenv

from modes import MODE_POLICIES

CLIENTS = ("cli", "discord", "line")


def _parse_args(args: list[str]) -> tuple[str, str]:
    usage = (
        f"usage: python3 main.py <{'|'.join(MODE_POLICIES)}> "
        f"<{'|'.join(CLIENTS)}>"
    )
    if len(args) != 2:
        raise SystemExit(usage)

    mode, client_name = args
    if client_name not in CLIENTS or mode not in MODE_POLICIES:
        raise SystemExit(usage)
    return client_name, mode


def main():
    name, mode = _parse_args(sys.argv[1:])

    load_dotenv()

    # Imported here rather than at module level so that starting one client
    # never requires the other one's dependencies or credentials to be present.
    if name == "cli":
        from clients.cli import run
    elif name == "discord":
        from clients.discord import run
    else:
        from clients.line import run
    run(mode)


if __name__ == "__main__":
    main()
