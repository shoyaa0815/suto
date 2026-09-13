import sys

from dotenv import load_dotenv

from application.modes import PUBLIC_MODES

INTERFACES = ("cli", "discord", "line")


def _parse_args(args: list[str]) -> tuple[str, str]:
    usage = (
        f"usage: python3 main.py <{'|'.join(PUBLIC_MODES)}> "
        f"<{'|'.join(INTERFACES)}>"
    )
    if len(args) != 2:
        raise SystemExit(usage)

    mode, interface_name = args
    if interface_name not in INTERFACES or mode not in PUBLIC_MODES:
        raise SystemExit(usage)
    return interface_name, mode


def main():
    name, mode = _parse_args(sys.argv[1:])

    load_dotenv()

    # Imported here rather than at module level so that starting one interface
    # never requires the other one's dependencies or credentials to be present.
    if name == "cli":
        from interfaces.tui import run
    elif name == "discord":
        from interfaces.discord import run
    else:
        from interfaces.line import run
    run(mode)


if __name__ == "__main__":
    main()
