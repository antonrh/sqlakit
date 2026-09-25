"""`sqlakit`, the command line."""

from __future__ import annotations

import argparse
import os
import sys

from ._sql import registered, signature_of


def main(argv: list[str] | None = None) -> int:
    """Run a command, and return what the shell should make of it."""
    parser = argparse.ArgumentParser(prog="sqlakit", description="SQLAKit.")
    commands = parser.add_subparsers(dest="command", required=True)

    macros = commands.add_parser(
        "macros", help="list the `tpl.` macros templates can call"
    )
    macros.add_argument(
        "modules",
        nargs="*",
        metavar="MODULE",
        help="a module whose macros to list too, or `module:name` for one of them",
    )
    macros.add_argument(
        "--markdown", action="store_true", help="write Markdown, for documentation"
    )
    macros.add_argument(
        "--namespace", default="tpl", help="the schema name calls are written under"
    )

    arguments = parser.parse_args(argv)
    if arguments.command == "macros":
        return _macros(
            arguments.modules,
            markdown=arguments.markdown,
            namespace=arguments.namespace,
        )
    return 1


def _macros(modules: list[str], *, markdown: bool, namespace: str) -> int:
    """Print every macro: the built-in ones, and those the modules define."""
    for macro in registered(modules).values():
        signature = signature_of(macro, namespace)
        if markdown:
            _say(f"### `{signature}`\n\n{macro.doc}\n")
        else:
            summary = macro.doc.split("\n", 1)[0]
            _say(f"{_paint(signature, BOLD)}\n    {summary}")
    return 0




BOLD = "1"


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






if __name__ == "__main__":
    sys.exit(main())
