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

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._sql import MacroTemplate, Templates
from .exceptions import (
    MacroArgumentError,
    MacroSyntaxError,
    ProjectConfigError,
    UnknownMacroError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["Problem", "Project", "load_project"]

_KEYS = {"paths", "macros", "namespace"}


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
        macros=config.get("macros", []),
        namespace=config.get("namespace", "tpl"),
    )
    return Project(root, templates)


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
