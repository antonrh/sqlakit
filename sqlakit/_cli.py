"""`sqlakit`, the command line."""

from __future__ import annotations

import argparse
import os
import sys

from ._debugserver import create_server


def main(argv: list[str] | None = None) -> int:
    """Run a command, and return what the shell should make of it."""
    parser = argparse.ArgumentParser(prog="sqlakit", description="SQLAKit.")
    commands = parser.add_subparsers(dest="command", required=True)

    debugserver = commands.add_parser(
        "debugserver", help="watch the recordings an application sends"
    )
    debugserver.add_argument("-H", "--host", default="localhost")
    debugserver.add_argument("-p", "--port", type=int, default=5555)

    arguments = parser.parse_args(argv)
    if arguments.command == "debugserver":
        return _debugserver(arguments.host, arguments.port)
    return 1


def _debugserver(host: str, port: int) -> int:
    """Serve the recordings until the terminal says otherwise."""
    try:
        server = create_server(host, port)
    except OSError as error:
        _say(f"\n{_paint('cannot listen', BOLD, RED)} on {host}:{port} — {error}\n")
        return 1
    _greeting(host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _say(_paint("\nstopped", DIM))
    finally:
        server.server_close()
    return 0


BOLD = "1"
DIM = "2"
TEAL = "36"
RED = "31"
GREEN = "32"
VIOLET = "35"


def _colours() -> bool:
    """Whether to paint: a terminal that wants it, and was not told otherwise."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty()


def _paint(text: str, *codes: str) -> str:
    """Return the text in those colours, or as it is where colour is unwanted."""
    if not codes or not _colours():
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def _say(text: str) -> None:
    """Print, and flush: the server then blocks, and a pipe would hold this."""
    print(text, flush=True)  # noqa: T201


def _greeting(host: str, port: int) -> None:
    """Print where the page is, and the block that fills it."""
    where = _paint(f"http://{host}:{port}", TEAL, BOLD)
    label = _paint('"GET /users"', GREEN)
    named = _paint(f'"{host}"', GREEN)
    number = _paint(str(port), VIOLET)
    _say(
        f"\nSQLAKit debug server on {where}\n\n"
        + _paint(
            "Send recordings to it:\n",
            DIM,
        )
        + f"\n    with db.recording({label}, debugserver=({named}, {number})):\n"
        + "        list_users()\n"
    )


if __name__ == "__main__":
    sys.exit(main())
