import sys


DEFAULT_MODE = "agent"


def _parse_args(args: list[str]) -> tuple[str, str]:
    usage = "usage: python3 main.py [settings|api]"
    if not args:
        return "cli", DEFAULT_MODE
    if args == ["settings"]:
        return "settings_web", "settings"
    if args == ["api"]:
        return "api", DEFAULT_MODE
    raise SystemExit(usage)


def main():
    name, mode = _parse_args(sys.argv[1:])

    from dotenv import load_dotenv

    load_dotenv()
    if name == "cli":
        from interfaces.cli import run
    elif name == "settings_web":
        from interfaces.web import run
    else:
        from interfaces.api import run
    run(mode)


if __name__ == "__main__":
    main()
