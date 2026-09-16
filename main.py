import sys

from application.modes import PUBLIC_MODES

INTERFACES = ("cli", "discord", "line")
INTERFACE_MODES = tuple(mode for mode in PUBLIC_MODES if mode != "home")


def _parse_args(args: list[str]) -> tuple[str, str]:
    usage = (
        "usage: python3 main.py <home|settings|web> | "
        f"python3 main.py <{'|'.join(INTERFACE_MODES)}> "
        f"<{'|'.join(INTERFACES)}>"
    )
    if args == ["home"]:
        return "home_terminal", "home"
    if args == ["settings"]:
        return "settings_terminal", "settings"
    if args == ["web"]:
        return "web", "web"
    if len(args) != 2:
        raise SystemExit(usage)

    mode, interface_name = args
    if interface_name not in INTERFACES or mode not in INTERFACE_MODES:
        raise SystemExit(usage)
    return interface_name, mode


def main():
    name, mode = _parse_args(sys.argv[1:])

    # Imported here rather than at module level so that starting one interface
    # never requires the other one's dependencies or credentials to be present.
    if name == "home_terminal":
        from interfaces.home_terminal import run

        run(mode)
        return

    from dotenv import load_dotenv

    load_dotenv()
    if name == "settings_terminal":
        from interfaces.settings_terminal import run
    elif name == "cli":
        from interfaces.cli import run
    elif name == "discord":
        from interfaces.discord import run
    elif name == "web":
        from interfaces.web import run
    else:
        from interfaces.line import run
    run(mode)


if __name__ == "__main__":
    main()
