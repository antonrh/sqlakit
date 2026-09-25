"""`sqlakit`, the command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ._sql import registered, signature_of
from .editor._lsp import serve
from .editor._project import Problem, load_project
from .editor._pycharm import DIALECTS, ddl, dialects
from .editor._sqruff import settings, stale
from .exceptions import ProjectConfigError


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

    check = commands.add_parser(
        "check", help="check every template the project's pyproject.toml names"
    )
    check.add_argument(
        "--project",
        default=".",
        help="a directory in the project, the current one by default",
    )
    check.add_argument("--format", choices=("text", "json"), default="text")

    commands.add_parser(
        "lsp", help="run the language server for SQL templates, over stdio"
    )

    export = commands.add_parser(
        "export", help="write what another tool needs to read the templates"
    )
    export.add_argument("tool", choices=("sqruff", "pycharm"))
    export.add_argument(
        "--project",
        default=".",
        help="a directory in the project, the current one by default",
    )
    export.add_argument("--dialect", help="the dialect, when pyproject.toml says none")
    export.add_argument(
        "--check",
        action="store_true",
        help="write nothing, and fail if what is written is out of date",
    )

    arguments = parser.parse_args(argv)
    if arguments.command == "check":
        return _check(Path(arguments.project), json_output=arguments.format == "json")
    if arguments.command == "export":
        export = _export_pycharm if arguments.tool == "pycharm" else _export
        return export(
            Path(arguments.project), dialect=arguments.dialect, check=arguments.check
        )
    if arguments.command == "lsp":  # pragma: no cover - run by an editor
        serve()
        return 0
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


def _check(directory: Path, *, json_output: bool) -> int:
    """Print what is wrong with the project's templates, and fail if anything is."""
    try:
        project = load_project(directory)
    except ProjectConfigError as error:
        _say(str(error))
        return 2
    problems = list(project.problems())
    if json_output:
        _say(json.dumps([_as_json(problem) for problem in problems], indent=2))
    else:
        for problem in problems:
            line, column = problem.position()
            _say(f"{_relative(problem.path)}:{line}:{column}: {problem.message}")
        count = len(project.templates.names())
        _say(_paint(f"{count} templates, {len(problems)} problems", DIM))
    return 1 if problems else 0


def _export(directory: Path, *, dialect: str | None, check: bool) -> int:
    """Write the `sqruff` settings that read the templates into `pyproject.toml`."""
    try:
        project = load_project(directory)
    except ProjectConfigError as error:
        _say(str(error))
        return 2
    pyproject = project.root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
    if check:
        tables = stale(project, text)
        if tables:
            _say(f"{', '.join(tables)} out of date: run `sqlakit export sqruff`")
            return 1
        return 0
    pyproject.write_text(settings(project, text, dialect), encoding="utf-8")
    _say(f"wrote {_relative(pyproject)}")
    return 0


def _export_pycharm(directory: Path, *, dialect: str | None, check: bool) -> int:
    """Write what PyCharm needs into `.idea/`: the macros as DDL, and the dialect."""
    try:
        project = load_project(directory)
    except ProjectConfigError as error:
        _say(str(error))
        return 2
    chosen = (dialect or project.dialect or "postgresql").lower()
    if chosen not in DIALECTS:
        _say(
            f"PyCharm has no dialect `{chosen}`: pass one of {', '.join(sorted(DIALECTS))}"
        )
        return 2
    idea = project.root / ".idea"
    ddl_path = idea / "sqlakit.sql"
    mappings_path = idea / "sqldialects.xml"
    written = (
        mappings_path.read_text(encoding="utf-8") if mappings_path.exists() else None
    )
    wanted = {
        ddl_path: ddl(project, "snowflake" if chosen == "snowflake" else "postgresql"),
        mappings_path: dialects(
            project.root,
            [Path(path) for path in project.templates.paths],
            chosen,
            written,
        ),
    }
    if check:
        stale = [
            _relative(path)
            for path, text in wanted.items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]
        if stale:
            _say(f"{', '.join(stale)} out of date: run `sqlakit export pycharm`")
            return 1
        return 0
    idea.mkdir(exist_ok=True)
    for path, text in wanted.items():
        path.write_text(text, encoding="utf-8")
    _say(
        f"wrote {_relative(ddl_path)} and {_relative(mappings_path)}\n\n"
        "Once, in PyCharm:\n"
        f"  Database > + > DDL Data Source, and add {_relative(ddl_path)} to it\n"
        "  Settings > Tools > Database > User Parameters: add the pattern\n"
        r"    :(\w+(?:\.\w+)*)  with SQL checked"
    )
    return 0


def _as_json(problem: Problem) -> dict[str, object]:
    line, column = problem.position()
    return {
        "path": _relative(problem.path),
        "line": line,
        "column": column,
        "message": problem.message,
    }


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd()).as_posix()
    except ValueError:
        return str(path)




BOLD = "1"
DIM = "2"


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
