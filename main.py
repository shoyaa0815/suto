import os
import sys


DEFAULT_MODE = "agent"


def _parse_args(args: list[str]) -> tuple[str, str]:
    usage = "usage: python3 main.py [setting|api|voice|worker]"
    if not args:
        return "cli", DEFAULT_MODE
    if args == ["setting"]:
        return "setting", DEFAULT_MODE
    if args == ["api"]:
        return "api", DEFAULT_MODE
    if args == ["voice"]:
        return "voice", DEFAULT_MODE
    if args == ["worker"]:
        return "worker", DEFAULT_MODE
    raise SystemExit(usage)


def main():
    name, mode = _parse_args(sys.argv[1:])

    from dotenv import load_dotenv

    load_dotenv()
    if os.environ.get("SUTO_MCP_CONFIG"):
        print("MCP stdio servers are trusted local executables running with your user permissions.",
              file=sys.stderr)
    if name == "cli":
        from interfaces.cli import run
    elif name == "setting":
        from interfaces.web import run
    elif name == "voice":
        from interfaces.voice import run
    elif name == "worker":
        from application.worker import run
    else:
        from interfaces.api import run
    run(mode)


if __name__ == "__main__":
    main()
