"""A project's templates and macros, read from its code without running it.

The language server and the command line need what `Templates(...)` holds:
where the templates are, which macros they call, and under which namespace.
The application already says so in its code, so nothing repeats it: the code
is parsed, never imported.

- A function decorated `@sql_macro` is a macro. Its name, arguments,
  docstring and line come from the parse, which is all a check or an editor
  needs of it.
- A call to `Templates(...)`, or a `templates=` given to a database, names the
  directories, the files of SQL macros and the namespace. Paths are worked
  out as far as the code spells them plainly: a string, `Path(__file__)`,
  `.parent`, `/`, and a name the module assigned one of those to.
- When no call says where the templates are, every directory named `sql` is.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._sql import NAMESPACE, Context, Macro, Param, Sql, _Slot
from .exceptions import MacroArgumentError

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["Discovered", "StaticMacro", "discover"]

SKIPPED = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "test",
    "tests",
    "venv",
}
"""Directories that hold no code of the application's own."""


class StaticMacro(Macro):
    """A Python macro as its source says it is, for checking a call to it.

    It is never called: a template that calls it is checked, not rendered.
    """

    def __init__(  # noqa: PLR0913 - what a definition says
        self,
        name: str,
        *,
        slots: tuple[_Slot, ...],
        variadic: _Slot | None,
        context: bool,
        optional: bool,
        lazy: bool,
        doc: str,
        path: Path,
        line: int,
    ) -> None:
        self.name = name.lower()
        self.slots = slots
        self.variadic = variadic
        self.context = context
        self.optional = optional
        self.lazy = lazy
        self.doc = doc
        self.path = path
        self.line = line
        self.func = self._unread

    def _unread(self, *_: Any) -> str:  # noqa: ANN401
        problem = "was read from its source, and is not run"
        raise MacroArgumentError(self.name, problem)


@dataclass
class Discovered:
    """The templates and the macros a project's code names."""

    paths: list[Path] = field(default_factory=list)
    macros: list[Macro | Path] = field(default_factory=list)
    namespace: str = NAMESPACE


def discover(root: Path) -> Discovered:
    """Read the templates and the macros of the project under ``root``."""
    found = Discovered()
    for path in _python_files(root):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names = _assigned(module)
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef) and (macro := _macro_of(node, path)):
                found.macros.append(macro)
            elif isinstance(node, ast.Call):
                _read_call(node, names, path, root, found)
    if not found.paths:
        found.paths = sorted(
            directory
            for directory in root.rglob("sql")
            if directory.is_dir() and not _skipped(directory, root)
        )
    return found


def _python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if not _skipped(path, root):
            yield path


def _skipped(path: Path, root: Path) -> bool:
    return any(part in SKIPPED for part in path.relative_to(root).parts[:-1])


def _assigned(module: ast.Module) -> dict[str, ast.expr]:
    """Return what the module assigns to each name at its top level."""
    names: dict[str, ast.expr] = {}
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            names[node.target.id] = node.value
    return names


def _read_call(
    node: ast.Call,
    names: dict[str, ast.expr],
    path: Path,
    root: Path,
    found: Discovered,
) -> None:
    """Take what a `Templates(...)` or a `templates=` says."""
    called = _last_name(node.func)
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    if called == "Templates":
        where = node.args[0] if node.args else keywords.get("path")
        _add_paths(where, names, path, root, found)
        for macro in _values(keywords.get("macros"), names, path, root):
            if isinstance(macro, Path) and macro.suffix == ".sql":
                found.macros.append(macro)
        namespace = keywords.get("namespace")
        if isinstance(namespace, ast.Constant) and isinstance(namespace.value, str):
            found.namespace = namespace.value
    elif "templates" in keywords and not isinstance(keywords["templates"], ast.Call):
        _add_paths(keywords["templates"], names, path, root, found)


def _add_paths(
    node: ast.expr | None,
    names: dict[str, ast.expr],
    path: Path,
    root: Path,
    found: Discovered,
) -> None:
    for value in _values(node, names, path, root):
        directory = root / value if isinstance(value, str) else value
        if (
            isinstance(directory, Path)
            and directory.is_dir()
            and directory not in found.paths
        ):
            found.paths.append(directory)


def _values(
    node: ast.expr | None, names: dict[str, ast.expr], path: Path, root: Path
) -> list[Any]:
    """Return what a path, or a list of them, works out to, as far as it can."""
    if isinstance(node, ast.List | ast.Tuple):
        return [one for item in node.elts for one in _values(item, names, path, root)]
    value = _value(node, names, path, root)
    return [] if value is None else [value]


def _value(  # noqa: C901, PLR0911 - one case per way code spells a path
    node: ast.expr | None,
    names: dict[str, ast.expr],
    path: Path,
    root: Path,
    depth: int = 0,
) -> Any:  # noqa: ANN401
    """Return the path an expression spells, a string, or None for anything else.

    A plain string is a path from the project's root, where the application
    runs from, unless it names a module: `app.sql.macros` stays a string.
    """
    if node is None or depth > 10:  # noqa: PLR2004 - a name assigned to itself
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
        return root / text if "/" in text or text.endswith(".sql") else text
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return path
        return _value(names.get(node.id), names, path, root, depth + 1)
    if isinstance(node, ast.Call) and _last_name(node.func) == "Path" and node.args:
        inner = _value(node.args[0], names, path, root, depth + 1)
        return Path(inner) if isinstance(inner, str | Path) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in ("resolve", "absolute"):
            return _value(node.func.value, names, path, root, depth + 1)
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = _value(node.value, names, path, root, depth + 1)
        return inner.parent if isinstance(inner, Path) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _value(node.left, names, path, root, depth + 1)
        right = _value(node.right, names, path, root, depth + 1)
        if isinstance(left, Path) and isinstance(right, str | Path):
            return left / (
                right.relative_to(root) if isinstance(right, Path) else right
            )
        return None
    return None


def _last_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _macro_of(node: ast.FunctionDef, path: Path) -> StaticMacro | None:
    """Return the macro a function decorated `@sql_macro` defines, if it is one."""
    for decorator in node.decorator_list:
        called = decorator.func if isinstance(decorator, ast.Call) else decorator
        if _last_name(called) != "sql_macro":
            continue
        options = {
            keyword.arg: keyword.value
            for keyword in (
                decorator.keywords if isinstance(decorator, ast.Call) else []
            )
        }
        return _read_macro(node, path, options)
    return None


def _read_macro(
    node: ast.FunctionDef, path: Path, options: dict[str | None, ast.expr]
) -> StaticMacro:
    arguments = [*node.args.posonlyargs, *node.args.args]
    defaults = [None] * (len(arguments) - len(node.args.defaults)) + list(
        node.args.defaults
    )
    context = False
    slots = []
    for index, (argument, default) in enumerate(zip(arguments, defaults, strict=True)):
        kind, choices = _kind(argument.annotation)
        if kind is Context and index == 0:
            context = True
            continue
        slots.append(
            _Slot(
                argument.arg,
                kind,
                inspect.Parameter.empty if default is None else ast.unparse(default),
                choices,
            )
        )
    variadic = None
    if node.args.vararg is not None:
        kind, choices = _kind(node.args.vararg.annotation)
        variadic = _Slot(node.args.vararg.arg, kind, choices=choices)
    name = options.get("name")
    named = name.value if isinstance(name, ast.Constant) else None
    return StaticMacro(
        named if isinstance(named, str) else node.name,
        slots=tuple(slots),
        variadic=variadic,
        context=context,
        optional=_flag(options.get("optional")),
        lazy=_flag(options.get("lazy")),
        doc=ast.get_docstring(node) or "",
        path=path,
        line=node.decorator_list[0].lineno if node.decorator_list else node.lineno,
    )


def _kind(annotation: ast.expr | None) -> tuple[Any, tuple[str, ...]]:
    """Return what an annotation makes an argument, and the SQL a `Literal` allows."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        annotation = ast.parse(annotation.value, mode="eval").body
    if (
        isinstance(annotation, ast.Subscript)
        and _last_name(annotation.value) == "Literal"
    ):
        items = annotation.slice
        elements = items.elts if isinstance(items, ast.Tuple) else [items]
        choices = tuple(
            element.value
            for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
        return Sql, choices
    named = _last_name(annotation) if annotation is not None else ""
    return {"Param": Param, "Context": Context}.get(named, Sql), ()


def _flag(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True
