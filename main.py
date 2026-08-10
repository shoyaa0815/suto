import sys

from dotenv import load_dotenv

CLIENTS = ("discord", "line")


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "discord"
    if name not in CLIENTS:
        raise SystemExit(f"usage: python main.py [{'|'.join(CLIENTS)}]")

    load_dotenv()

    # Imported here rather than at module level so that starting one client
    # never requires the other one's dependencies or credentials to be present.
    if name == "discord":
        from clients.discord import run
    else:
        from clients.line import run
    run()


if __name__ == "__main__":
    main()
