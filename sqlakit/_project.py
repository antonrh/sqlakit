"""A project's templates, as its `pyproject.toml` describes them.

The command line and the language server have no `Database` to ask, so they
read where the templates are, and which macros they call, from the project:

```toml
[tool.sqlakit.templates]
paths = ["app/sql"]
macros = ["app.sql.macros"]
namespace = "tpl"
```
"""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._sql import MacroTemplate, SqlMacro, Templates, sql_macros
from .exceptions import (
    MacroArgumentError,
    MacroDefinitionError,
    MacroSyntaxError,
    ProjectConfigError,
    UnknownMacroError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["Problem", "Project", "load_project"]

_KEYS = {"paths", "macros", "namespace", "dialect"}

_PARAMETER_IN_TEXT = re.compile(
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|/\*.*?\*/"""
    r"|(?<![:\w\\]):([A-Za-z_]\w*)(\.\w+)?",
    re.DOTALL,
)
"""A `:parameter`, past the strings and comments that may hold a colon."""

LINT_EXCLUDED = ("RF01", "AL05", "ST03")
"""Rules a `tpl.` call trips without anything being wrong with the template.

`RF01` reads `tpl.if_set` as a column of a table named `tpl`, and `AL05` and
`ST03` miss an alias or a CTE used only inside a macro's argument.
"""


@dataclass(frozen=True, slots=True)
class Problem:
    """Something wrong with a template: the file, where in it, and what."""

    path: Path
    start: int
    end: int
    message: str

    def position(self) -> tuple[int, int]:
        """Return the line and column the problem starts at, both from one."""
        source = self.path.read_text(encoding="utf-8")
        line = source.count("\n", 0, self.start) + 1
        column = self.start - (source.rfind("\n", 0, self.start) + 1) + 1
        return line, column


@dataclass(frozen=True, slots=True)
class Project:
    """The directory `pyproject.toml` is in, and the templates it describes."""

    root: Path
    templates: Templates
    dialect: str | None = None
    """The dialect the templates are written in, for a linter to read them in."""

    def name_of(self, path: Path) -> str | None:
        """Return a file's template name, or None when no template path holds it."""
        resolved = path.resolve()
        for root in self.templates.paths:
            try:
                return resolved.relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                continue
        return None

    def path_of(self, name: str) -> Path | None:
        """Return the file a template name reads, or None when there is none."""
        for root in self.templates.paths:
            path = Path(root) / name
            if path.is_file():
                return path
        return None

    def load(self, name: str, source: str) -> MacroTemplate:
        """Read a macro template from its text, which may not be saved yet.

        Raises:
            MacroSyntaxError: if it cannot be read.
            UnknownMacroError: if it calls a macro nobody registered.
            MacroArgumentError: if a call has arguments its macro cannot take.

        """
        engine = self.templates.engine
        return MacroTemplate(
            name,
            source,
            self.templates.macros,
            namespace=self.templates.namespace,
            load=engine._read,  # noqa: SLF001 - the engine reads its includes
        )

    def problems(self) -> Iterator[Problem]:
        """Check every template, and yield what is wrong with each."""
        for name in self.templates.names():
            path = self.path_of(name)
            assert path is not None  # noqa: S101 - `names` lists files
            try:
                self.load(name, path.read_text(encoding="utf-8"))
            except (MacroSyntaxError, UnknownMacroError, MacroArgumentError) as error:
                if error.chain:
                    continue  # in a template this one includes, and said there
                start, end = error.span or (0, 0)
                yield Problem(path, start, end, str(error))
        for path in self.macro_files():
            source = path.read_text(encoding="utf-8")
            for line, message in self.macro_problems(path, source):
                start = _line_start(source, line)
                yield Problem(path, start, start, message)

    def placeholder_values(self) -> dict[str, str]:
        """Return a value to stand for each parameter, for a linter to read SQL.

        `1` reads wherever a value goes, `LIMIT :limit` included. A parameter
        read with a path, `:criteria.search`, stands for its own name instead,
        which reads as a column.
        """
        values: dict[str, str] = {}
        for name in self.templates.names():
            path = self.path_of(name)
            assert path is not None  # noqa: S101 - `names` lists files
            for found in _PARAMETER_IN_TEXT.finditer(path.read_text(encoding="utf-8")):
                param, dotted = found.groups()
                if param is not None:
                    values[param] = (
                        param if dotted or values.get(param) == param else "1"
                    )
        return dict(sorted(values.items()))

    def macro_files(self) -> list[Path]:
        """Return the files the project's SQL macros are written in."""
        found = {
            macro.path
            for macro in self.templates.macros.values()
            if isinstance(macro, SqlMacro)
        }
        return sorted(found)

    def macro_problems(self, path: Path, source: str) -> list[tuple[int, str]]:
        """Return the line and the problem of each broken macro in a file of them.

        ``source`` is the file's text, which an editor may hold unsaved.
        """
        namespace = self.templates.namespace
        try:
            written = sql_macros(path, namespace, source)
        except MacroDefinitionError as error:
            return [(1, str(error))]
        macros = {**self.templates.macros, **{one.name: one for one in written}}
        found = []
        for macro in written:
            try:
                MacroTemplate(
                    macro.source_name,
                    macro.expanded(list(macro.params)),
                    macros,
                    namespace=namespace,
                    load=self.templates.engine._read,  # noqa: SLF001
                    expanding=(macro.name,),
                    first_line=macro.body_line,
                )
            except (MacroSyntaxError, UnknownMacroError, MacroArgumentError) as error:
                line = error.chain[0][1] if error.chain else error.line
                found.append((line, str(error)))
        return found


def _line_start(source: str, line: int) -> int:
    """Return the offset a line starts at, counting lines from one."""
    start = 0
    for _ in range(line - 1):
        newline = source.find("\n", start)
        if newline < 0:
            break
        start = newline + 1
    return start


def load_project(start: Path | None = None) -> Project:
    """Return the project `start` is in, from the nearest `pyproject.toml` up.

    Its directory goes on `sys.path`, so that `macros` import the way the
    application imports them.

    Raises:
        ProjectConfigError: if there is no `pyproject.toml`, it has no
            `[tool.sqlakit.templates]`, or that has keys it does not take.

    """
    pyproject = _pyproject(start or Path.cwd())
    config = _section(pyproject)
    root = pyproject.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    paths = [root / path for path in config.get("paths", [])]
    templates = Templates(
        paths,
        macros=[
            str(root / macro) if macro.endswith(".sql") else macro
            for macro in config.get("macros", [])
        ],
        namespace=config.get("namespace", "tpl"),
    )
    return Project(root, templates, config.get("dialect"))


def _pyproject(start: Path) -> Path:
    for directory in (start.resolve(), *start.resolve().parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    problem = f"there is no `pyproject.toml` in {start} or above it"
    raise ProjectConfigError(problem)


def _section(pyproject: Path) -> dict[str, Any]:
    with pyproject.open("rb") as file:
        data = tomllib.load(file)
    config = data.get("tool", {}).get("sqlakit", {}).get("templates")
    if config is None:
        problem = f"`{pyproject}` has no `[tool.sqlakit.templates]` table"
        raise ProjectConfigError(problem)
    unknown = sorted(set(config) - _KEYS)
    if unknown:
        problem = (
            f"`[tool.sqlakit.templates]` in `{pyproject}` has "
            f"{', '.join(unknown)}, and takes {', '.join(sorted(_KEYS))}"
        )
        raise ProjectConfigError(problem)
    return config
