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
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql.base import (
    RESERVED_WORDS as POSTGRESQL_RESERVED_WORDS,
)
from sqlalchemy.sql.compiler import RESERVED_WORDS

from ._sql import (
    SAMPLE_AFTER,
    STAGE_AFTER,
    MacroTemplate,
    SqlMacro,
    Templates,
    sql_macros,
)
from ._static import discover
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

_RESERVED = RESERVED_WORDS | POSTGRESQL_RESERVED_WORDS
"""Names SQL keeps for itself, which a linter cannot read a parameter as."""

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
        """Return a value for each parameter a linter cannot read by its name.

        With no value, the `placeholder` templater writes a parameter's name in
        its place: `:status` reads as a column. Where a stage goes, after `LIST`,
        `PUT <file>` or `COPY INTO ... FROM`, a linter reads only `@stage/path`,
        and inside `SAMPLE (...)` only a number. A name SQL reserves reads as
        neither, `LIMIT :limit` reading `LIMIT limit`, so it gets `1`, or its
        name with `_` after it when a path follows, `:order.id`.
        """
        values: dict[str, str] = {}
        paths = [self.path_of(name) for name in self.templates.names()]
        for path in {*(one for one in paths if one is not None), *self.macro_files()}:
            placeholders_of(path.read_text(encoding="utf-8"), values)
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
        try:
            written = sql_macros(path, source)
        except MacroDefinitionError as error:
            return [(1, str(error))]
        macros = {**self.templates.macros, **{one.name: one for one in written}}
        found = []
        for macro in written:
            try:
                self.templates.check_macro(macro, macros)
            except (MacroSyntaxError, UnknownMacroError, MacroArgumentError) as error:
                line = error.chain[0][1] if error.chain else error.line
                found.append((line, str(error)))
        return found


def placeholders_of(text: str, values: dict[str, str]) -> dict[str, str]:
    """Add to ``values`` what a linter needs for each parameter of the text.

    See `Project.placeholder_values`, which reads every template with it.
    """
    for found in _PARAMETER_IN_TEXT.finditer(text):
        param, dotted = found.groups()
        if param is None:
            continue
        before = text[: found.start()]
        if STAGE_AFTER.search(before):
            values[param] = "@stage/path"
        elif SAMPLE_AFTER.search(before):
            values[param] = "10"
        elif param.lower() in _RESERVED:
            values.setdefault(param, f"{param}_" if dotted else "1")
    return values


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
    """Return the project `start` is in, read from its code without running it.

    The root is the directory of the nearest `pyproject.toml` up, or `start`.
    The `Templates(...)` the code builds gives the template directories, the
    files of SQL macros and the namespace, and the functions it decorates
    `@sql_macro` give the Python macros: see `discover`.
    `[tool.sqlakit.templates]` says where to look instead, for code that
    builds the paths in a way the reading cannot follow.

    Raises:
        ProjectConfigError: if no template directory is found, or the table has
            keys it does not take.

    """
    start = (start or Path.cwd()).resolve()
    pyproject = _pyproject(start)
    root = pyproject.parent if pyproject is not None else start
    config = _section(pyproject) if pyproject is not None else {}
    found = discover(root)
    paths = (
        [root / path for path in config["paths"]] if "paths" in config else found.paths
    )
    if not paths:
        problem = (
            f"no code under {root} builds `Templates(...)` with a path it can read, "
            f"and no directory there is named `sql`"
        )
        raise ProjectConfigError(problem)
    sql_macros = (
        [root / macro for macro in config["macros"] if macro.endswith(".sql")]
        if "macros" in config
        else [macro for macro in found.macros if isinstance(macro, Path)]
    )
    python_macros = [macro for macro in found.macros if not isinstance(macro, Path)]
    templates = Templates(
        paths,
        macros=[*python_macros, *sql_macros],
        namespace=config.get("namespace", found.namespace),
    )
    return Project(root, templates, config.get("dialect"))


def _pyproject(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _section(pyproject: Path) -> dict[str, Any]:
    with pyproject.open("rb") as file:
        data = tomllib.load(file)
    config = data.get("tool", {}).get("sqlakit", {}).get("templates", {})
    unknown = sorted(set(config) - _KEYS)
    if unknown:
        problem = (
            f"`[tool.sqlakit.templates]` in `{pyproject}` has "
            f"{', '.join(unknown)}, and takes {', '.join(sorted(_KEYS))}"
        )
        raise ProjectConfigError(problem)
    return config
