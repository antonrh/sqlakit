"""A project's templates and macros, read from its code without running it.

The language server and the command line need what `Templates(...)` holds:
where the templates are, which macros they call, and under which namespace.
The code is parsed, never imported.

- A function decorated `@sql_macro` is a macro. Its name, arguments,
  docstring and line come from the parse, which is all a check or an editor
  needs of it.
- A call to `Templates(...)`, or a `templates=` given to a database, names the
  directories, the files of SQL macros and the namespace. Paths are worked
  out as far as the code spells them plainly: a string, `Path(__file__)`,
  `.parent`, `/`, and a name the module assigned one of those to.
- When no call says where the templates are, every directory named `sql` is.
- A database URL written as a string, `Database("postgresql://...")` or the
  default of `os.environ.get(...)`, gives the dialect.
- The namespace is a string, or a name assigned or imported from a module of
  the project. When the code passes it some other way, the templates' calls of
  the built-in macros tell it.

Each finding keeps the file and the line it came from, so a check can say
what it read and where.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._sql import INCLUDE, NAMESPACE, Context, Macro, Param, Sql, _Slot, registered
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
        column: int = 0,
        sql_path: Path | None = None,
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
        self.sql_path = sql_path
        """The file its SQL is in, for one declared `@sql_macro("file.sql")`."""
        self.name_at = (line, column)
        """The line and the column of the function's name, as an editor goes to it."""
        self.func = self._unread

    def _unread(self, *_: Any) -> str:  # noqa: ANN401
        problem = "was read from its source, and is not run"
        raise MacroArgumentError(self.name, problem)


@dataclass
class Discovered:
    """The templates and the macros a project's code names, and where it does."""

    paths: list[Path] = field(default_factory=list)
    macros: list[Macro | Path] = field(default_factory=list)
    namespace: str = NAMESPACE
    unread_namespace: str | None = None
    """Where the code passes a namespace in a way the reading cannot follow."""
    dialect: str | None = None
    origins: dict[Path | str, str] = field(default_factory=dict)
    """Where each path, `namespace` and `dialect` was read: `shop/db.py:8`."""


def discover(root: Path) -> Discovered:
    """Read the templates and the macros of the project under ``root``."""
    found = Discovered()
    for path in _python_files(root):
        try:
            source = path.read_text(encoding="utf-8")
            module = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names = _assigned(module)
        for node in ast.walk(module):
            if isinstance(node, ast.FunctionDef) and (
                macro := _macro_of(node, path, source)
            ):
                found.macros.append(macro)
            elif isinstance(node, ast.Call):
                _read_call(node, names, path, root, found)
    if found.unread_namespace is not None and "namespace" not in found.origins:
        _guess_namespace(found)
    if not found.paths:
        found.paths = sorted(
            directory
            for directory in root.rglob("sql")
            if directory.is_dir() and not _skipped(directory, root)
        )
        for directory in found.paths:
            found.origins[directory] = "a directory named `sql`"
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
    """Take what a `Templates(...)`, a `templates=` or a database URL says."""
    called = _last_name(node.func)
    keywords = {keyword.arg: keyword.value for keyword in node.keywords}
    at = f"{path.relative_to(root).as_posix()}:{node.lineno}"
    if called == "Templates":
        where = node.args[0] if node.args else keywords.get("path")
        _add_paths(where, names, path, root, found, at=at)
        for macro in _values(keywords.get("macros"), names, path, root):
            if isinstance(macro, Path) and macro.suffix == ".sql":
                found.macros.append(macro)
        if (namespace := keywords.get("namespace")) is not None:
            text = _text_of(namespace, names, path, root)
            if text is None:
                found.unread_namespace = at
            else:
                found.namespace = text
                found.origins["namespace"] = at
        return
    if called == "Database" and found.dialect is None:
        url = node.args[0] if node.args else keywords.get("url")
        if dialect := _dialect_of(url, names):
            found.dialect = dialect
            found.origins["dialect"] = at
    if "templates" in keywords and not isinstance(keywords["templates"], ast.Call):
        _add_paths(keywords["templates"], names, path, root, found, at=at)


def _text_of(
    node: ast.expr | None,
    names: dict[str, ast.expr],
    path: Path,
    root: Path,
    depth: int = 0,
) -> str | None:
    """Return the string an expression spells, as far as the reading follows it.

    A literal, a name the module assigns one to, or a name it imports from a
    module of the project: `from .settings import NAMESPACE`, or
    `settings.NAMESPACE` after `from . import settings`.
    """
    if node is None or depth > 10:  # noqa: PLR2004 - a name assigned to itself
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name) and node.id in names:
        return _text_of(names[node.id], names, path, root, depth + 1)
    if isinstance(node, ast.Name):
        module, name = _imported(path, root).get(node.id, (None, None))
        if module is not None and name is not None:
            return _text_of(ast.Name(name), _names_in(module), module, root, depth + 1)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        module, name = _imported(path, root).get(node.value.id, (None, None))
        if module is not None and name is None:
            return _text_of(
                ast.Name(node.attr), _names_in(module), module, root, depth + 1
            )
    return None


def _names_in(path: Path) -> dict[str, ast.expr]:
    """Return what a module of the project assigns at its top level."""
    try:
        return _assigned(ast.parse(path.read_text(encoding="utf-8")))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}


def _imported(path: Path, root: Path) -> dict[str, tuple[Path, str | None]]:
    """Return what the module imports from the project: a name, or a module.

    `from .settings import NAMESPACE` maps `NAMESPACE` to the file and the name
    in it, and `from . import settings` maps `settings` to the file alone.
    """
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return {}
    found: dict[str, tuple[Path, str | None]] = {}
    for node in module.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        base = path.parent
        for _ in range(max(node.level - 1, 0)):
            base = base.parent
        if node.level == 0:
            base = root
        parts = node.module.split(".") if node.module else []
        for alias in node.names:
            local = alias.asname or alias.name
            if (file := _module_file(base.joinpath(*parts, alias.name))) is not None:
                found[local] = (file, None)
            elif (file := _module_file(base.joinpath(*parts))) is not None:
                found[local] = (file, alias.name)
    return found


def _module_file(stem: Path) -> Path | None:
    for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _guess_namespace(found: Discovered) -> None:
    """Take the namespace from the templates, when the code passes it unreadably.

    The prefix the templates call the built-in macros under is the namespace:
    `t.if_set(` and `t.include(` say `t`.
    """
    builtins = "|".join(sorted([*registered([]), INCLUDE], key=len, reverse=True))
    calls = re.compile(rf"(?<![\w.])([A-Za-z_]\w*)\.(?:{builtins})\s*\(", re.IGNORECASE)
    prefixes: Counter[str] = Counter()
    for directory in found.paths:
        for template in directory.rglob("*.sql"):
            text = template.read_text(encoding="utf-8", errors="replace")
            prefixes.update(match.group(1) for match in calls.finditer(text))
    unread = found.unread_namespace
    if prefixes:
        found.namespace = prefixes.most_common(1)[0][0]
        found.origins["namespace"] = (
            f"the templates' calls, as {unread} passes it in a way the reading "
            f"cannot follow"
        )
    else:
        found.origins["namespace"] = (
            f"the default, as {unread} passes it in a way the reading cannot "
            f"follow: set it in [tool.sqlakit.templates]"
        )


def _dialect_of(node: ast.expr | None, names: dict[str, ast.expr]) -> str | None:
    """Return the dialect a URL names, when the code writes the URL out.

    `"postgresql+psycopg://..."` is `postgresql`. A URL from the environment
    counts when the code gives a default: `os.environ.get("URL", "sqlite://")`.
    """
    if isinstance(node, ast.Name):
        node = names.get(node.id)
    if (
        isinstance(node, ast.Call)
        and _last_name(node.func) in ("get", "getenv")
        and len(node.args) > 1
    ):
        node = node.args[1]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        scheme, separator, _ = node.value.partition("://")
        if separator and scheme:
            return scheme.split("+", 1)[0]
    return None


def _add_paths(  # noqa: PLR0913 - the call, and where it was read
    node: ast.expr | None,
    names: dict[str, ast.expr],
    path: Path,
    root: Path,
    found: Discovered,
    *,
    at: str,
) -> None:
    for value in _values(node, names, path, root):
        directory = root / value if isinstance(value, str) else value
        if (
            isinstance(directory, Path)
            and directory.is_dir()
            and directory not in found.paths
        ):
            found.paths.append(directory)
            found.origins[directory] = at


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


def _macro_of(node: ast.FunctionDef, path: Path, source: str) -> StaticMacro | None:
    """Return the macro a function decorated `@sql_macro` defines, if it is one."""
    for decorator in node.decorator_list:
        called = decorator.func if isinstance(decorator, ast.Call) else decorator
        if _last_name(called) != "sql_macro":
            continue
        options: dict[str | None, ast.expr] = {
            keyword.arg: keyword.value
            for keyword in (
                decorator.keywords if isinstance(decorator, ast.Call) else []
            )
        }
        if isinstance(decorator, ast.Call) and decorator.args:
            options[None] = decorator.args[0]  # `@sql_macro("file.sql")`
        return _read_macro(node, path, options, source)
    return None


def _read_macro(
    node: ast.FunctionDef,
    path: Path,
    options: dict[str | None, ast.expr],
    source: str,
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
        line=node.lineno,
        column=_name_column(source, node),
        sql_path=_sql_path(options.get(None), path),
    )


def _sql_path(node: ast.expr | None, path: Path) -> Path | None:
    """Return the file a `@sql_macro("file.sql")` names, from the module's directory."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return None
    written = Path(node.value)
    return written if written.is_absolute() else path.parent / written


def _name_column(source: str, node: ast.FunctionDef) -> int:
    """Return the column of a function's name, in characters: `ast` counts bytes."""
    line = source.splitlines()[node.lineno - 1]
    written = line.encode()[: node.col_offset].decode(errors="ignore")
    return len(written) + len("def ")


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
