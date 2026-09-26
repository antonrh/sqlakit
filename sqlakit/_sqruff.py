"""The `sqruff` settings in `pyproject.toml` that read templates as SQL.

The `placeholder` templater writes each `:name` as `name`. Where that reads as
something else, `settings` gives the parameter a value; see
`Project.placeholder_values`. Which names a dialect keeps for itself is asked
of `sqruff`, when it is installed, since its dialects keep words SQLAlchemy's
do not: `exclude` and `row` on SQLite.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from typing import TYPE_CHECKING, Any

from ._project import LINT_EXCLUDED
from .exceptions import ProjectConfigError

if TYPE_CHECKING:
    from ._project import Project

__all__ = ["settings", "stale"]

_HEADER = re.compile(
    r"^\[tool\.sqruff\.templater\.placeholder\][ \t]*(?:#[^\n]*)?$", re.MULTILINE
)

_DIALECTS = {"postgresql": "postgres", "mariadb": "mysql", "mssql": "tsql"}
"""sqruff's name for a dialect SQLAlchemy names another way."""
_NEXT_TABLE = re.compile(r"^\[", re.MULTILINE)


def stale(project: Project, pyproject: str) -> list[str]:
    """Return the tables of `pyproject.toml` that no longer read the templates."""
    sqruff = tomllib.loads(pyproject).get("tool", {}).get("sqruff", {})
    written = sqruff.get("templater", {}).get("placeholder", {})
    values = project.placeholder_values(_kept(project, _dialect(sqruff, None, project)))
    checked = (
        ("[tool.sqruff.core]", "core" in sqruff),
        (
            "[tool.sqruff.templater.placeholder]",
            all(str(written.get(name)) == str(value) for name, value in values.items()),
        ),
    )
    return [table for table, fresh in checked if not fresh]


def settings(project: Project, pyproject: str, dialect: str | None) -> str:
    """Return `pyproject.toml` with the settings that read the templates.

    The values the templates need are written again each time, and a value
    added to the table by hand stays. `[tool.sqruff.core]` is written only when
    it is missing, so the rules set there stay.

    Raises:
        ProjectConfigError: if the table is written in a way this cannot rewrite.

    """
    sqruff = tomllib.loads(pyproject).get("tool", {}).get("sqruff", {})
    written = dict(sqruff.get("templater", {}).get("placeholder", {}))
    written.pop("param_style", None)
    text = pyproject
    chosen = _dialect(sqruff, dialect, project)
    if "core" not in sqruff:
        core = [
            "[tool.sqruff.core]",
            *([f'dialect = "{chosen}"'] if chosen else []),
            'templater = "placeholder"',
            'rules = "all"',
            f'exclude_rules = "{",".join(LINT_EXCLUDED)}"',
        ]
        text = text.rstrip("\n") + "\n\n" + "\n".join(core) + "\n"
    values = project.placeholder_values(_kept(project, chosen))
    merged = dict(sorted({**written, **values}.items()))
    table = "\n".join(
        [
            "[tool.sqruff.templater.placeholder]",
            "# `sqlakit export sqruff` writes what the templates need, and keeps",
            "# what you add.",
            'param_style = "colon"',
            *(f"{name} = {_toml_value(value)}" for name, value in merged.items()),
        ]
    )
    if found := _HEADER.search(text):
        following = _NEXT_TABLE.search(text, found.end())
        end = following.start() if following else len(text)
        after = "\n\n" if following else "\n"
        written_out = text[: found.start()] + table + after + text[end:]
    else:
        written_out = text.rstrip("\n") + "\n\n" + table + "\n"
    try:
        tomllib.loads(written_out)
    except tomllib.TOMLDecodeError as error:
        problem = (
            "`[tool.sqruff.templater.placeholder]` is written in a way this "
            f"cannot rewrite ({error}): give it a table of its own"
        )
        raise ProjectConfigError(problem) from error
    return written_out


def _dialect(sqruff: dict, given: str | None, project: Project) -> str | None:
    """Return the dialect `sqruff` reads the templates in, by its name."""
    if "core" in sqruff:
        # sqruff reads a table without a dialect as ANSI, and so does this.
        written = sqruff["core"].get("dialect")
        return written if isinstance(written, str) else None
    chosen = given or project.dialect
    return _DIALECTS.get(chosen or "", chosen)


def _kept(project: Project, dialect: str | None) -> set[str]:
    """Return the parameter names `sqruff` cannot read as a column in the dialect.

    Each name is linted as `SELECT <name>;`, one to a line, in one run. Without
    `sqruff` installed, the dialects of SQLAlchemy decide alone.
    """
    binary = shutil.which("sqruff")
    names = sorted(project.parameter_names())
    if binary is None or not names:
        return set()
    # JSON, as the report of its own is another shape on GitHub Actions.
    command = [binary, "lint", "--parsing-errors", "--format", "json", "-"]
    if dialect:
        command[2:2] = ["--dialect", dialect]
    source = "".join(f"SELECT {name};\n" for name in names)
    # Away from the project, so no setting of its own changes the reading.
    with tempfile.TemporaryDirectory() as empty:
        found = subprocess.run(  # noqa: S603 - sqruff, found on the PATH
            command,
            input=source,
            capture_output=True,
            text=True,
            cwd=empty,
            check=False,
        )
    lines = {
        problem["range"]["start"]["line"]
        for problems in _report(found.stdout, found.stderr).values()
        for problem in problems
        # The name starts after `SELECT `, at the eighth character.
        if problem.get("message") == "Unparsable section"
        and problem["range"]["start"]["character"] == len("SELECT ") + 1
    }
    return {name for number, name in enumerate(names, 1) if number in lines}


def _report(*outputs: str) -> dict[str, list[dict[str, Any]]]:
    """Return the JSON report, from whichever stream `sqruff` wrote it to."""
    for output in outputs:
        try:
            report = json.loads(output)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict):
            return report
    return {}


def _toml_value(value: object) -> str:
    """Return a value as TOML writes it: a string quoted, a flag in lower case."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value))
