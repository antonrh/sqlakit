from __future__ import annotations

import importlib
import importlib.util
import inspect
import re
from collections.abc import Mapping, Sized
from contextvars import ContextVar
from dataclasses import dataclass, is_dataclass
from functools import cache, cached_property, lru_cache
from inspect import iscoroutinefunction
from pathlib import Path
from types import SimpleNamespace
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    TypeVar,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

import sqlalchemy as sa

from ._discovery import import_string
from ._query import _field_named, _parse_sort_field
from .exceptions import (
    AsyncFilterError,
    Chain,
    InvalidSortStringError,
    MacroArgumentError,
    MacroDefinitionError,
    MacroSyntaxError,
    MissingDependencyError,
    ParameterPathError,
    SQLNotConfiguredError,
    StrayParameterError,
    TemplateNotFoundError,
    UnknownIdentifierError,
    UnknownMacroError,
    UnknownOrderFieldError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    import jinja2
    from jinja2sql import Jinja2SQL
    from markupsafe import Markup
    from sqlalchemy.sql import Executable

    from ._base import BaseDatabase
else:
    try:
        import jinja2
        from jinja2sql import Jinja2SQL
        from markupsafe import Markup
    except ImportError:  # pragma: no cover - the extra is installed in CI
        jinja2 = Jinja2SQL = Markup = None

if TYPE_CHECKING:
    from pydantic import BaseModel, TypeAdapter
else:
    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:  # pragma: no cover - pydantic is installed in CI
        BaseModel = TypeAdapter = None

__all__ = [
    "BUILTIN_MACROS",
    "BaseSQLQuery",
    "Context",
    "Filter",
    "JinjaEngine",
    "Macro",
    "MacroEngine",
    "Param",
    "Sql",
    "Templates",
    "require_pydantic",
    "signature_of",
    "sql_macro",
    "templates_of",
    "tpl",
]

_preparer: ContextVar[Any] = ContextVar("sqlakit.identifier_preparer")
"""The preparer of the database a template is rendering for."""

_context: ContextVar[Context] = ContextVar("sqlakit.macro_context")
"""The call a macro template is rendering, for a macro that calls another."""

RowT = TypeVar("RowT")
OtherT = TypeVar("OtherT")
SessionT = TypeVar("SessionT")
DatabaseT = TypeVar("DatabaseT", bound="BaseDatabase[Any, Any]")
QueryT = TypeVar("QueryT", bound="BaseSQLQuery[Any, Any]")

PathLike = str | Path
"""The directories templates are looked for in: one, or several."""


# Templates that stay SQL: `tpl.` macros in place of Jinja.
#
# A `.tpl.sql` file is SQL in the production dialect, with `:name` parameters.
# Each part that changes per call is a `tpl.<macro>(...)` call, which every SQL tool reads
# as a function of a schema named `tpl`. A file is cut into text and calls once,
# when it is first read; rendering joins the pieces and calls the macros.

SUFFIX = ".tpl.sql"
"""The extension that picks macros over Jinja."""

NAMESPACE = "tpl"
"""The schema name macros are called under unless `Templates` says otherwise."""
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
_PARAMETER = re.compile(r"\s*:([A-Za-z_]\w*(?:\.\w+)*)\s*")
_DOTTED = re.compile(
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|/\*.*?\*/|\$(\w*)\$.*?\$\1\$"""
    r"|(?<![:\w\\]):([A-Za-z_]\w*(?:\.\w+)+)",
    re.DOTALL,
)
"""A `:parameter.with.a.path`, past the strings and comments that may hold one."""
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")
_COLUMN_AS = re.compile(r"\s*([A-Za-z_]\w*)\s*=(?!=)\s*(.+?)\s*", re.DOTALL)
_NULLS_AFTER = re.compile(r"\s*NULLS\b", re.IGNORECASE)


class Param:
    """An argument written as `:name`: its name, and the value the call passed.

    `str(param)` is the placeholder, so a macro puts the parameter back into the
    SQL as it was written and it is bound like any other.
    """

    __slots__ = ("name", "value")

    def __init__(self, name: str, value: Any) -> None:  # noqa: ANN401
        self.name = name
        self.value = value

    def __str__(self) -> str:
        return f":{self.name}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r}, {self.value!r})"


class _Value(Param):
    """A value passed where a `:parameter` goes, bound when the SQL names it.

    A macro that only reads the value binds nothing, and one that binds each
    item of a list binds only those.
    """

    __slots__ = ("_ctx", "_macro", "_placeholder")

    def __init__(self, value: Any, ctx: Context | None, macro: str) -> None:  # noqa: ANN401
        super().__init__("value", value)
        self._ctx = ctx
        self._macro = macro
        self._placeholder: str | None = None

    def __str__(self) -> str:
        if self._placeholder is None:
            if self._ctx is None:
                raise _outside_a_template(self._macro)
            self._placeholder = self._ctx.bind(self.value)
        return self._placeholder


def _named(param: Param, *suffix: str) -> str | None:
    """Return the name to bind a value derived from a parameter under.

    `:q` binds its pattern as `:q__like__1`. A value passed in Python has no
    name of its own, and binds as `:__p1`.
    """
    if isinstance(param, _Value):
        return None
    return "__".join((param.name, *suffix))


def _outside_a_template(macro: str) -> MacroArgumentError:
    problem = "called outside a template, where there is nothing to bind to"
    return MacroArgumentError(macro, problem)


class Sql(str):
    """An argument as SQL text, with the macros inside it already expanded."""

    __slots__ = ()


class Context:
    """The call as a macro sees it, beyond its arguments.

    ``dialect`` is the name of the database's dialect, such as `postgresql` or
    `snowflake`. `bind` adds a value of the macro's own to the statement.
    """

    def __init__(
        self,
        dialect: str,
        preparer: Any,  # noqa: ANN401 - SQLAlchemy's IdentifierPreparer
        values: Mapping[str, Any],
    ) -> None:
        self.dialect = dialect
        self.preparer = preparer
        self.values: dict[str, Any] = dict(values)
        self._bound = 0

    no_order = "(SELECT NULL)"
    """An `ORDER BY` term that orders by nothing, for a macro with nothing to sort by.

    Every database takes it after `ORDER BY`, and a direction after it: `NULL`
    and `NULL DESC` are refused by PostgreSQL, and `0` is a column's position.
    """

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.dialect!r})"

    def bind(self, value: Any, name: str | None = None) -> str:  # noqa: ANN401
        """Bind a value, and return the placeholder to write in its place.

        ``name`` names the value in the log and the debug server: `search_like`
        binds `:search_like__1`, and no name binds `:__p1`.
        """
        while True:
            self._bound += 1
            key = f"{name}__{self._bound}" if name else f"__p{self._bound}"
            if key not in self.values:
                break
        self.values[key] = value
        return f":{key}"

    def quote(self, name: str) -> str:
        """Return an identifier quoted the way this database quotes one."""
        return self.preparer.quote(name)


@dataclass(frozen=True, slots=True)
class _Slot:
    """One parameter of a macro, as a call fills it."""

    name: str
    kind: type[Param | Sql]
    default: Any = inspect.Parameter.empty
    choices: tuple[str, ...] = ()
    """The SQL a `Literal` annotation lets the argument be, written as in the template."""

    @property
    def required(self) -> bool:
        return self.default is inspect.Parameter.empty


class Macro:
    """A function that `tpl.<name>(...)` calls, made by `sql_macro`.

    It stays callable as the function it wraps.
    """

    def __init__(
        self,
        func: Callable[..., str],
        name: str | None = None,
        *,
        optional: bool = False,
    ) -> None:
        self.func = func
        self.name = (name or getattr(func, "__name__", "")).lower()
        self.doc = inspect.getdoc(func) or ""
        self.optional = optional
        if iscoroutinefunction(func):
            raise MacroDefinitionError(
                self.name,
                "it is a coroutine function, and templates render synchronously, "
                "in the async API as well. Await the value first, and pass it in "
                "the context",
            )
        self.context, self.slots, self.variadic = _slots_of(func, self.name)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    def __call__(self, *args: Any) -> str:  # noqa: ANN401
        """Call the macro from another one, the way a template calls it.

        ```python
        tpl.icontains("name", q)
        tpl.each([1, 2, 3])
        ```

        The context is the render's, a string is SQL, and a value where a
        `:parameter` goes is bound as one.

        Raises:
            MacroArgumentError: if it needs a context and no template is rendering.

        """
        if args and isinstance(args[0], Context):
            ctx, args = args[0], args[1:]
        else:
            ctx = _context.get(None)
        if ctx is None and self.context:
            raise _outside_a_template(self.name)
        converted = [self._converted(index, arg, ctx) for index, arg in enumerate(args)]
        return self.func(*([ctx] if self.context else []), *converted)

    def _converted(self, index: int, arg: Any, ctx: Context | None) -> Any:  # noqa: ANN401
        kind = self._kind_or_none(index)
        if kind is Param and not isinstance(arg, Param):
            return _Value(arg, ctx, self.name)
        if kind is Sql and not isinstance(arg, Sql):
            return Sql(arg)
        return arg

    def _kind_or_none(self, index: int) -> type[Param | Sql] | None:
        """Return what an argument is, or None past what the macro takes."""
        if index < len(self.slots) or self.variadic is not None:
            return self.kind_at(index)
        return None

    @property
    def minimum(self) -> int:
        return sum(slot.required for slot in self.slots)

    @property
    def maximum(self) -> float:
        return float("inf") if self.variadic else len(self.slots)

    def kind_at(self, index: int) -> type[Param | Sql]:
        return self.slot_at(index).kind

    def slot_at(self, index: int) -> _Slot:
        if index < len(self.slots):
            return self.slots[index]
        assert self.variadic is not None  # noqa: S101 - the count was checked
        return self.variadic


def sql_macro(
    func: Callable[..., str] | None = None,
    /,
    *,
    name: str | None = None,
    optional: bool = False,
) -> Any:  # noqa: ANN401
    """Make a function a macro that `.tpl.sql` templates call as `tpl.<name>(...)`.

    ```python
    @sql_macro
    def for_accounts(vendor_ids: Param, subaccount_ids: Param) -> str:
        return f"(vendor_id IN {vendor_ids} OR subaccount_id IN {subaccount_ids})"
    ```

    The annotations say what each argument is: a `Param` is written `:name` and
    brings its value, a `Sql` is the argument's text, and a `Context` first
    brings the dialect and a way to bind values of the macro's own.

    A `:name` the call did not pass is an error, unless ``optional`` is set: then
    it reads as `None`, for a macro whose point is that it may be missing.

    Raises:
        MacroDefinitionError: if a parameter is annotated as none of them.

    """
    if func is None:
        return lambda func: Macro(func, name, optional=optional)
    return Macro(func, name, optional=optional)


def _slots_of(
    func: Callable[..., Any], name: str
) -> tuple[bool, tuple[_Slot, ...], _Slot | None]:
    """Return whether a macro takes the context, its slots, and its `*args`."""
    hints = get_type_hints(func)
    context = False
    slots: list[_Slot] = []
    variadic = None
    for index, parameter in enumerate(inspect.signature(func).parameters.values()):
        kind = hints.get(parameter.name)
        if kind is Context and index == 0:
            context = True
            continue
        choices: tuple[str, ...] = ()
        if get_origin(kind) is Literal and all(
            isinstance(choice, str) for choice in get_args(kind)
        ):
            kind, choices = Sql, get_args(kind)
        if kind not in (Param, Sql):
            raise MacroDefinitionError(
                name,
                f"`{parameter.name}` is annotated `{kind}`: annotate it `Param`, "
                f"`Sql`, a `Literal` of the SQL it may be, or `Context` as the "
                f"first parameter",
            )
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            variadic = _Slot(parameter.name, kind, choices=choices)
        elif parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            if kind is Param and parameter.default is not inspect.Parameter.empty:
                raise MacroDefinitionError(
                    name, f"`{parameter.name}` is a `Param` with a default"
                )
            slots.append(_Slot(parameter.name, kind, parameter.default, choices))
        else:
            raise MacroDefinitionError(
                name, f"`{parameter.name}` is keyword-only, and calls in SQL are not"
            )
    return context, tuple(slots), variadic


def signature_of(macro: Macro, namespace: str = NAMESPACE) -> str:
    """Return how a template calls a macro: `tpl.if_set(:p, expr[, otherwise])`."""
    written = ""
    for index, slot in enumerate(macro.slots):
        shown = f":{slot.name}" if slot.kind is Param else slot.name
        separator = ", " if index else ""
        written += f"{separator}{shown}" if slot.required else f"[{separator}{shown}]"
    if macro.variadic is not None:
        separator = ", " if macro.slots else ""
        shown = macro.variadic.name
        written += f"{separator}*{':' if macro.variadic.kind is Param else ''}{shown}"
    return f"{namespace}.{macro.name}({written})"


# The template, cut into text and calls.


@dataclass(frozen=True, slots=True)
class _Arg:
    parts: tuple[str | _Call, ...]
    param: str | None
    """The name, when the argument is `:name` and nothing else."""
    span: tuple[int, int]
    """The argument's place in the template, without the space around it."""


@dataclass(frozen=True, slots=True)
class _Call:
    name: str
    args: tuple[_Arg, ...]
    line: int
    span: tuple[int, int]
    """The call's place in the template, from the namespace to its `)`."""


@cache
def _patterns(namespace: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Return where the scanner has something to decide, and what a call is."""
    prefix = re.escape(namespace)
    decide = re.compile(
        rf"""['"$(\[{{)\]}},]|--|/\*|(?<![\w.]){prefix}\.""", re.IGNORECASE
    )
    call = re.compile(rf"{prefix}\.([A-Za-z_]\w*)\s*\(", re.IGNORECASE)
    return decide, call


class _Scanner:
    """Cut a template into text and `tpl.` calls, past strings and comments."""

    def __init__(
        self, source: str, template: str, namespace: str, chain: Chain = ()
    ) -> None:
        self.source = source
        self.template = template
        self.namespace = namespace
        self.chain = chain
        self.decide, self.call = _patterns(namespace)

    def parts(self) -> tuple[str | _Call, ...]:
        return self._scan(0, in_args=False)[0]

    def _scan(
        self, start: int, *, in_args: bool
    ) -> tuple[tuple[str | _Call, ...], int, str | None]:
        """Read up to the end, or to the `,` or `)` that ends an argument."""
        source = self.source
        parts: list[str | _Call] = []
        text_from = index = start
        depth = 0
        while found := self.decide.search(source, index):
            index = found.start()
            char = source[index]
            if (skipped := self.past_literal(index)) != index:
                index = skipped
            elif (call := self.call.match(source, index)) and not self._inside_name(
                index
            ):
                parts.append(source[text_from:index])
                args, index = self._args(call.end())
                parts.append(
                    _Call(
                        call.group(1).lower(),
                        args,
                        self._line(call.start()),
                        (call.start(), index),
                    )
                )
                text_from = index
            elif in_args and char in "([{":
                depth += 1
                index += 1
            elif in_args and char in ")]}" and depth:
                depth -= 1
                index += 1
            elif in_args and (char == ")" or (char == "," and not depth)):
                parts.append(source[text_from:index])
                return _joined(parts), index, char
            else:
                index += 1
        parts.append(source[text_from:])
        return _joined(parts), len(source), None

    def _args(self, start: int) -> tuple[tuple[_Arg, ...], int]:
        """Read a call's arguments, and return where the call ends."""
        args = []
        index = start
        while True:
            begin = index
            parts, index, closer = self._scan(index, in_args=True)
            if closer is None:
                problem = f"a `{self.namespace}.` call is never closed"
                raise self._error(problem, start - 1)
            args.append(_argument(parts, self._trimmed(begin, index)))
            index += 1
            if closer == ")":
                break
        if len(args) == 1 and args[0].parts == ():
            args = []
        return tuple(args), index

    def past_literal(self, index: int) -> int:
        """Return where a string, a quoted name or a comment starting here ends.

        When none starts there, the offset comes back unchanged.
        """
        source = self.source
        if source[index] in "'\"":
            return self._past_quoted(index, source[index])
        dollar = _DOLLAR_QUOTE.match(source, index)
        if dollar and not self._inside_name(index):
            return self._past(dollar.group(), dollar.end(), "a dollar-quoted string")
        if source.startswith("--", index):
            newline = source.find("\n", index)
            return len(source) if newline < 0 else newline
        if source.startswith("/*", index):
            return self._past("*/", index + 2, "a comment")
        return index

    def _past(self, closer: str, start: int, what: str) -> int:
        """Return where ``closer`` ends, or say that ``what`` is never closed."""
        end = self.source.find(closer, start)
        if end < 0:
            problem = f"{what} is never closed"
            raise self._error(problem, start)
        return end + len(closer)

    def _past_quoted(self, start: int, quote: str) -> int:
        """Return where a string or a quoted name ends; a doubled quote is inside it."""
        index = start + 1
        while True:
            index = self.source.find(quote, index)
            if index < 0:
                problem = "a quoted string is never closed"
                raise self._error(problem, start)
            if self.source.startswith(quote * 2, index):
                index += 2
                continue
            return index + 1

    def _inside_name(self, index: int) -> bool:
        """Whether `tpl.` here is the tail of a longer name, as in `x.tpl.f(`."""
        before = self.source[index - 1] if index else ""
        return before == "." or before.isalnum() or before == "_"

    def _trimmed(self, start: int, end: int) -> tuple[int, int]:
        """Return a span without the space at either end."""
        text = self.source[start:end]
        lead = len(text) - len(text.lstrip())
        return start + lead, max(start + lead, end - (len(text) - len(text.rstrip())))

    def _line(self, index: int) -> int:
        return self.source.count("\n", 0, index) + 1

    def _error(self, problem: str, index: int) -> MacroSyntaxError:
        return MacroSyntaxError(
            self.template,
            self._line(index),
            problem,
            chain=self.chain,
            span=(index, index + 1),
        )


def _joined(parts: list[str | _Call]) -> tuple[str | _Call, ...]:
    return tuple(part for part in parts if part != "")


def _argument(parts: tuple[str | _Call, ...], span: tuple[int, int]) -> _Arg:
    """Return an argument, trimmed, and whether it is a parameter alone."""
    trimmed = list(parts)
    if trimmed and isinstance(trimmed[0], str):
        trimmed[0] = trimmed[0].lstrip()
    if trimmed and isinstance(trimmed[-1], str):
        trimmed[-1] = trimmed[-1].rstrip()
    trimmed_parts = _joined(trimmed)
    param = None
    if len(trimmed_parts) == 1 and isinstance(trimmed_parts[0], str):
        match = _PARAMETER.fullmatch(trimmed_parts[0])
        param = match.group(1) if match else None
    return _Arg(trimmed_parts, param, span)


# Loading and rendering.


INCLUDE = "include"
"""The call that puts a whole query from another template in place, as `(...)`."""

_QUOTED_PATH = re.compile(r"'([^']+)'")


class MacroTemplate:
    """A `.tpl.sql` file, read and checked against the macros it calls.

    `tpl.include('other.tpl.sql')` puts the query of another template in its
    place, in parentheses, where a table goes:

    ```sql
    SELECT f.fan_id FROM tpl.include('audience-fan/ids.tpl.sql') AS f
    ```

    The path is a string, read with the file: a template that is missing, or
    that includes itself through others, is refused there. The included query
    shares the parameters of the call, and loses a trailing `;`.
    """

    def __init__(  # noqa: PLR0913 - what a template is read with
        self,
        name: str,
        source: str,
        macros: Mapping[str, Macro],
        mtime: float = 0,
        namespace: str = NAMESPACE,
        *,
        load: Callable[[str], tuple[str, float]] | None = None,
        chain: tuple[tuple[str, int], ...] = (),
    ) -> None:
        self.name = name
        self.mtime = mtime
        self.macros = macros
        self.namespace = namespace
        self.load = load
        self.chain = chain
        """The templates this one was included from, and the line of each call."""
        self.includes: dict[str, float] = {}
        """Every template this one includes, however deep, and when it changed."""
        self.paths: dict[str, tuple[str, ...]] = {}
        """Every `:a.b` read here, by the name it binds as, `a__b`, and its path."""
        self.parts = _Scanner(source, name, namespace, chain).parts()
        self._check(self.parts)
        self._compiled = self._compile(self.parts)

    def render(self, ctx: Context) -> str:
        """Return the SQL for this call, the values it bound going to ``ctx``."""
        for key, (root, *path) in self.paths.items():
            if root in ctx.values and key not in ctx.values:
                ctx.values[key] = _followed(ctx.values[root], root, path)
        token = _context.set(ctx)
        try:
            return self._render(self._compiled, ctx)
        finally:
            _context.reset(token)

    def _compile(self, parts: Sequence[str | _Call]) -> tuple[str | _Expansion, ...]:
        """Resolve every call to its macro once, so rendering only calls them.

        An included template is read here, and its pieces become these.
        """
        compiled: list[str | _Expansion] = []
        for part in parts:
            if isinstance(part, str):
                compiled.append(_DOTTED.sub(self._flattened, part))
            elif part.name == INCLUDE:
                # On a line of its own: the query may end in a `--` comment.
                compiled.extend(("(", *self._include(part), "\n)"))
            else:
                compiled.append(self._expansion(part))
        return tuple(compiled)

    def _include(self, call: _Call) -> tuple[str | _Expansion, ...]:
        path = _QUOTED_PATH.fullmatch(self._written(call.args[0].parts))
        assert path is not None  # noqa: S101 - checked on load
        name = path.group(1)
        chain = (*self.chain, (self.name, call.line))
        if name in {template for template, _ in chain}:
            cycle = " -> ".join((*(template for template, _ in chain), name))
            raise self._refuse(INCLUDE, f"includes itself: {cycle}", call)
        if self.load is None:
            problem = "has no template paths to read from"
            raise self._refuse(INCLUDE, problem, call)
        try:
            source, mtime = self.load(name)
        except MacroArgumentError as error:
            raise self._refuse(INCLUDE, error.problem, call) from None
        included = MacroTemplate(
            name,
            _without_semicolon(
                source,
                _Scanner(source, name, self.namespace, chain),
            ),
            self.macros,
            mtime,
            self.namespace,
            load=self.load,
            chain=chain,
        )
        self.includes.update({name: mtime, **included.includes})
        self.paths.update(included.paths)
        return included._compiled

    def _flattened(self, match: re.Match[str]) -> str:
        """Write `:a.b` as the parameter it binds as, and leave the rest alone."""
        path = match.group(2)
        return match.group() if path is None else f":{self._key(path)}"

    def _key(self, param: str) -> str:
        """Return the name a parameter binds as: `a.b` as `a__b`, `a` as itself."""
        if "." not in param:
            return param
        path = tuple(param.split("."))
        key = "__".join(path)
        self.paths[key] = path
        return key

    def _expansion(self, call: _Call) -> _Expansion:
        macro = self.macros[call.name]
        args = tuple(
            (self._key(arg.param), None)
            if macro.kind_at(index) is Param and arg.param is not None
            else (None, self._compile(arg.parts))
            for index, arg in enumerate(call.args)
        )
        params = tuple(param for param, _ in args if param is not None)
        return _Expansion(macro, call.line, args, params, self.name, self.chain)

    def _check(self, parts: Sequence[str | _Call]) -> None:
        """Refuse an unknown macro or a call it cannot take, where the file is read.

        Raises:
            UnknownMacroError: naming the macro and what there is.
            MacroArgumentError: if a call has the wrong arguments.

        """
        for index, part in enumerate(parts):
            if isinstance(part, str):
                continue
            after = parts[index + 1] if index + 1 < len(parts) else ""
            if (
                part.name == "order_by"
                and isinstance(after, str)
                and _NULLS_AFTER.match(after)
            ):
                problem = (
                    "a NULLS after the call would follow a NULLS of a sort string. "
                    "Pass the default as an argument: 'nulls_last' or 'nulls_first'"
                )
                raise self._refuse(part.name, problem, part)
            if part.name == INCLUDE:
                self._check_include(part)
                continue
            macro = self.macros.get(part.name)
            if macro is None:
                raise UnknownMacroError(
                    part.name,
                    self.name,
                    part.line,
                    [*self.macros, INCLUDE],
                    namespace=self.namespace,
                    chain=self.chain,
                    span=part.span,
                )
            count = len(part.args)
            if not macro.minimum <= count <= macro.maximum:
                problem = f"takes {_arity(macro)} arguments, got {count}"
                raise self._refuse(macro.name, problem, part)
            for position, arg in enumerate(part.args):
                slot = macro.slot_at(position)
                written = self._written(arg.parts)
                if slot.kind is Param and arg.param is None:
                    problem = (
                        f"argument {position + 1} must be a :parameter, got {written!r}"
                    )
                    raise self._refuse(macro.name, problem, part, arg.span)
                if slot.choices and written not in slot.choices:
                    allowed = " or ".join(slot.choices)
                    problem = f"argument {position + 1} is {allowed}, got {written}"
                    raise self._refuse(macro.name, problem, part, arg.span)
                self._check(arg.parts)

    def _check_include(self, call: _Call) -> None:
        written = [self._written(arg.parts) for arg in call.args]
        if len(written) != 1 or not _QUOTED_PATH.fullmatch(written[0]):
            problem = (
                "takes the path of a template as a string, such as "
                f"'reports/ids.tpl.sql', got {', '.join(written)!r}"
            )
            raise self._refuse(INCLUDE, problem, call)

    def _render(self, parts: Sequence[str | _Expansion], ctx: Context) -> str:
        return "".join(
            [
                part if isinstance(part, str) else self._expand(part, ctx)
                for part in parts
            ]
        )

    def _expand(self, call: _Expansion, ctx: Context) -> str:
        macro = call.macro
        values = ctx.values
        missing = [name for name in call.params if name not in values]
        if missing and not macro.optional:
            problem = f"`:{missing[0]}` was not passed"
            raise call.refuse(problem, self.namespace)
        # An optional macro reads what was not passed as None, and so do the
        # calls inside its arguments: `if_set(:q, icontains(name, :q))`.
        values.update(dict.fromkeys(missing))
        try:
            return macro.func(*self._arguments(call, ctx))
        except MacroArgumentError as error:
            if error.template:
                raise
            # A macro's own refusal, said where the call is.
            raise call.refuse(error.problem, self.namespace) from None
        finally:
            for name in missing:
                del values[name]

    def _refuse(
        self,
        macro: str,
        problem: str,
        call: _Call,
        span: tuple[int, int] | None = None,
    ) -> MacroArgumentError:
        return MacroArgumentError(
            macro,
            problem,
            self.name,
            call.line,
            namespace=self.namespace,
            chain=self.chain,
            span=span or call.span,
        )

    def _written(self, parts: Sequence[str | _Call]) -> str:
        return "".join(
            part if isinstance(part, str) else f"{self.namespace}.{part.name}(...)"
            for part in parts
        )

    def _arguments(self, call: _Expansion, ctx: Context) -> list[Any]:
        args: list[Any] = [ctx] if call.macro.context else []
        for param, parts in call.args:
            if param is not None:
                args.append(Param(param, ctx.values[param]))
            else:
                args.append(Sql(self._render(parts or (), ctx)))
        return args


@dataclass(frozen=True, slots=True)
class _Expansion:
    """A call resolved to its macro: each argument a parameter's name, or parts."""

    macro: Macro
    line: int
    args: tuple[tuple[str | None, tuple[str | _Expansion, ...] | None], ...]
    params: tuple[str, ...]
    template: str
    """The template the call is written in, which an include makes another."""
    chain: Chain

    def refuse(self, problem: str, namespace: str) -> MacroArgumentError:
        return MacroArgumentError(
            self.macro.name,
            problem,
            self.template,
            self.line,
            namespace=namespace,
            chain=self.chain,
        )


def _followed(value: Any, root: str, path: Sequence[str]) -> Any:  # noqa: ANN401
    """Return what `:root.path` reads: a key of a mapping, an attribute otherwise.

    Raises:
        ParameterPathError: if a step names nothing there.

    """
    read = [root]
    for step in path:
        read.append(step)
        try:
            value = value[step] if isinstance(value, Mapping) else getattr(value, step)
        except (KeyError, AttributeError):
            raise ParameterPathError(".".join(read), step) from None
    return value


def _without_semicolon(source: str, scanner: _Scanner) -> str:
    """Return a query without the `;` that ends it, which a subquery cannot hold.

    The `;` is the last thing in the query that is not space or a comment:
    `SELECT 1; -- done` loses it too.
    """
    index, last = 0, -1
    while index < len(source):
        skipped = scanner.past_literal(index)
        if skipped == index:
            if not source[index].isspace():
                last = index
            index += 1
            continue
        if source[index] not in "-/":  # a string or a quoted name, not a comment
            last = skipped - 1
        index = skipped
    if last >= 0 and source[last] == ";":
        source = source[:last] + source[last + 1 :]
    return source.rstrip()


def _arity(macro: Macro) -> str:
    """Return how many arguments a macro takes, as a message says it."""
    if macro.variadic:
        return f"at least {macro.minimum}"
    if macro.minimum == macro.maximum:
        return str(macro.minimum)
    return f"{macro.minimum} to {int(macro.maximum)}"


class MacroEngine:
    """Renders templates with `tpl.` macros, each file read once and kept."""

    def __init__(
        self,
        paths: Sequence[Path | str],
        macros: Mapping[str, Macro],
        *,
        auto_reload: bool = False,
        namespace: str = NAMESPACE,
        every_file: bool = False,
    ) -> None:
        self.paths = tuple(Path(path) for path in paths)
        self.macros = macros
        self.auto_reload = auto_reload
        self.namespace = namespace
        self.every_file = every_file
        """Whether a `.sql` file is a macro template too, and not only `.tpl.sql`."""
        self._loaded: dict[str, MacroTemplate] = {}
        # A string is read once too: the same few are written out again and again.
        self._from_string = lru_cache(maxsize=256)(
            lambda source: MacroTemplate(
                "<string>", source, self.macros, namespace=namespace, load=self._read
            )
        )

    def render_file(
        self,
        name: str,
        context: Mapping[str, Any],
        preparer: Any,  # noqa: ANN401
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template file, and the values to bind to it."""
        return _rendered(self.get(name), context, preparer)

    def render_string(
        self,
        source: str,
        context: Mapping[str, Any],
        preparer: Any,  # noqa: ANN401
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template written out, and the values to bind to it."""
        return _rendered(self._from_string(source), context, preparer)

    def check(self, names: Iterable[str]) -> None:
        """Read these templates, which checks every call in them."""
        for name in names:
            self.get(name)

    def get(self, name: str) -> MacroTemplate:
        """Return a template by its name under the paths.

        Raises:
            TemplateNotFoundError: if no path holds it.

        """
        loaded = self._loaded.get(name)
        if loaded is not None and not (self.auto_reload and self._changed(loaded)):
            return loaded
        path = self._find(name)
        loaded = MacroTemplate(
            name,
            path.read_text(encoding="utf-8"),
            self.macros,
            path.stat().st_mtime,
            self.namespace,
            load=self._read,
        )
        self._loaded[name] = loaded
        return loaded

    def _changed(self, template: MacroTemplate) -> bool:
        """Whether the file, or a file it includes, changed since it was read."""
        files = {template.name: template.mtime, **template.includes}
        try:
            return any(
                self._find(name).stat().st_mtime != mtime
                for name, mtime in files.items()
            )
        except TemplateNotFoundError:
            return True

    def _read(self, name: str) -> tuple[str, float]:
        """Return an included template's source, and when it changed.

        Raises:
            MacroArgumentError: if it is not under the paths, or is a Jinja one.

        """
        if not (self.every_file or name.endswith(SUFFIX)):
            problem = (
                f"`{name}` is a Jinja template, and only a `{SUFFIX}` one is included"
            )
            raise MacroArgumentError(INCLUDE, problem)
        try:
            path = self._find(name)
        except TemplateNotFoundError as error:
            raise MacroArgumentError(INCLUDE, str(error).rstrip(".")) from None
        return path.read_text(encoding="utf-8"), path.stat().st_mtime

    def _find(self, name: str) -> Path:
        pieces = name.split("/")
        if ".." in pieces or name.startswith("/"):
            raise TemplateNotFoundError(name, self.paths)
        for root in self.paths:
            path = root.joinpath(*pieces)
            if path.is_file():
                return path
        raise TemplateNotFoundError(name, self.paths)


def _rendered(
    template: MacroTemplate,
    context: Mapping[str, Any],
    preparer: Any,  # noqa: ANN401
) -> tuple[str, Mapping[str, Any]]:
    dialect = getattr(getattr(preparer, "dialect", None), "name", "")
    ctx = Context(dialect, preparer, context)
    return template.render(ctx), ctx.values


def registered(macros: Iterable[Macro | str]) -> dict[str, Macro]:
    """Return the built-in macros and these, by name.

    A string is where to import them from: `app.sql.macros` for every macro of
    that module, `app.sql.macros:for_accounts` for one of them.

    Raises:
        MacroDefinitionError: if two macros share a name, or a path names
            something that is not a macro.
        UnknownImportPathError: if a path names nothing that can be imported.

    """
    by_name = dict(BUILTIN_MACROS)
    for macro in (
        found
        for given in macros
        for found in (_imported_macros(given) if isinstance(given, str) else [given])
    ):
        if not isinstance(macro, Macro):
            raise MacroDefinitionError(
                getattr(macro, "__name__", repr(macro)),
                "it is not decorated @sql_macro",
            )
        if macro.name in by_name or macro.name == INCLUDE:
            raise MacroDefinitionError(macro.name, "another macro has that name")
        by_name[macro.name] = macro
    return by_name


def _imported_macros(path: str) -> list[Any]:
    """Return the macro a path names, or every macro of the module it names."""
    if ":" not in path and _is_module(path):
        module = importlib.import_module(path)
        return [value for value in vars(module).values() if isinstance(value, Macro)]
    return [import_string(path)]


def _is_module(path: str) -> bool:
    try:
        return importlib.util.find_spec(path) is not None
    except ModuleNotFoundError:  # a parent that is a module, not a package
        return False


# The built-in macros.


@sql_macro(optional=True)
def if_set(value: Param, expr: Sql, otherwise: Sql = Sql("TRUE")) -> str:  # noqa: B008
    """`expr` when the parameter holds a value, `otherwise` when it does not.

    `None`, an empty string, an empty list and `False` hold none, and so does a
    parameter the call did not pass. `otherwise` is `TRUE`, which leaves a
    `WHERE` or an `AND` as though the condition were not there.
    """
    return expr if _is_set(value.value) else otherwise


@sql_macro(optional=True)
def unless_set(value: Param, expr: Sql, otherwise: Sql = Sql("TRUE")) -> str:  # noqa: B008
    """`expr` when the parameter holds no value, `otherwise` when it does.

    The other way round from `if_set`, for what applies only when a filter is
    not given: `AND tpl.unless_set(:status, status <> 'archived')`.
    """
    return otherwise if _is_set(value.value) else expr


def _is_set(value: Any) -> bool:  # noqa: ANN401
    """Whether a value is there: not None, not False, and not empty. `0` is there."""
    if value is None or value is False:
        return False
    return not isinstance(value, Sized) or len(value) > 0


@sql_macro
def order_by(ctx: Context, sort: Param, column: Sql, *columns: Sql) -> str:
    """`ORDER BY` terms from sort strings: `name`, `name.desc`, `name.desc.nulls_last`.

    One string or a list of them, sorting only by the columns listed after the
    parameter: the names come from a request, and nothing else reaches the SQL.
    A sort string names a column by its last part, `u.name` as `name`, and
    `name = <expression>` sorts by something else under that name:

    ```sql
    ORDER BY tpl.order_by(:order_by, id, name = name COLLATE 'und-ci-ai', 'nulls_last')
    ```

    `'nulls_last'` or `'nulls_first'` places the nulls of every term whose sort
    string does not say. MySQL has no `NULLS LAST`, and sorts by `IS NULL` first. `(SELECT NULL)`, which orders by nothing, when there is
    nothing to sort by: PostgreSQL refuses a bare `NULL`.
    """
    nulls_options = {"'nulls_first'", "'nulls_last'"}
    written = [one.strip() for one in (column, *columns)]
    options = [
        one.strip("'").lower() for one in written if one.lower() in nulls_options
    ]
    default_nulls = options[-1] if options else None
    offered = _offered(one for one in written if one.lower() not in nulls_options)
    requested = sort.value
    if not requested:
        return ctx.no_order
    fields = [requested] if isinstance(requested, str) else list(requested)
    terms = []
    for field in fields:
        name, descending, nulls = _parse_sort_field(str(field))
        direction = str(field).split(".")[1:2]
        if [one.lower() for one in direction] not in ([], ["asc"], ["desc"]) or (
            nulls not in (None, "nulls_first", "nulls_last")
        ):
            raise InvalidSortStringError(str(field))
        expression = offered[_field_named(name, offered)]
        nulls = nulls or default_nulls
        terms.append(
            _sort_term(ctx.dialect, expression, descending=descending, nulls=nulls)
        )
    return ", ".join(terms) or ctx.no_order


def _sort_term(
    dialect: str, expression: str, *, descending: bool, nulls: str | None
) -> str:
    """Return one `ORDER BY` term, with its nulls placed the way the dialect can."""
    term = f"{expression} {'DESC' if descending else 'ASC'}"
    if nulls is None:
        return term
    last = nulls == "nulls_last"
    if dialect in ("mysql", "mariadb"):
        return f"{expression} IS NULL {'ASC' if last else 'DESC'}, {term}"
    return f"{term} NULLS {'LAST' if last else 'FIRST'}"


def _offered(columns: Iterable[str]) -> dict[str, str]:
    """Return the columns a template lists, by the name a request asks for each.

    `u.name` is asked for as `name`, and `name = <expression>` is the expression
    under that name.
    """
    offered = {}
    for column in columns:
        if named := _COLUMN_AS.fullmatch(column):
            offered[named.group(1)] = named.group(2)
        else:
            offered[_last_name(column)] = column.strip()
    return offered


def _last_name(column: str) -> str:
    """Return the name a column is asked for by: `u.name` is `name`."""
    return column.strip().rsplit(".", 1)[-1].strip('"`[]')


@sql_macro
def icontains(
    ctx: Context,
    column: Sql,
    text: Sql,
    collation: Sql = Sql("'en-ci'"),  # noqa: B008
) -> str:
    """Whether a column holds the text anywhere, regardless of case.

    The text is a parameter or any expression: `icontains(city, spaced(:name))`.
    `ILIKE` on PostgreSQL, `CONTAINS(COLLATE(...))` on Snowflake, and `lower()`
    on both sides with `LIKE` elsewhere. `%` and `_` in the text match only
    themselves: the database escapes them before it compares. ``collation`` is
    the one Snowflake compares under, `'en-ci-ai'` to ignore accents as well;
    the other databases ignore it.
    """
    if ctx.dialect == "snowflake":
        return f"CONTAINS(COLLATE({column}, {collation}), {text})"
    param = _PARAMETER.fullmatch(text)
    if param and param.group(1) in ctx.values:
        # A parameter alone is escaped here, and its pattern bound in its place.
        value = str(ctx.values[param.group(1)])
        escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        pattern = ctx.bind(f"%{escaped}%", f"{param.group(1)}__like")
    elif ctx.dialect in ("mysql", "mariadb"):
        pattern = f"CONCAT('%', {_escaped_like(text)}, '%')"
    else:
        pattern = f"'%' || {_escaped_like(text)} || '%'"
    if ctx.dialect == "postgresql":
        return f"{column} ILIKE {pattern} ESCAPE '!'"
    return f"lower({column}) LIKE lower({pattern}) ESCAPE '!'"


def _escaped_like(text: str) -> str:
    """Return SQL that escapes `!`, `%` and `_` in the text, for `LIKE ... ESCAPE '!'`."""
    return f"replace(replace(replace({text}, '!', '!!'), '%', '!%'), '_', '!_')"


@sql_macro
def icollate(
    ctx: Context,
    column: Sql,
    collation: Sql = Sql("'en-ci'"),  # noqa: B008
) -> str:
    """Write a column so that it compares and sorts without regard to case.

    ```sql
    ORDER BY tpl.order_by(:sort, id, name = tpl.icollate(name), 'nulls_last')
    WHERE tpl.icollate(email) = tpl.icollate(:email)
    ```

    `COLLATE` on Snowflake, under ``collation``: `'en-ci'` unless it says
    `'und-ci-ai'` to ignore accents as well. `lower()` elsewhere, which minds
    accents, and the column as it is on MySQL, which compares without regard
    to case already.

    In a `GROUP BY` or a `DISTINCT` it changes more than the order: `COLLATE`
    groups `A` with `a` on Snowflake, and `lower()` returns `a` for both.
    """
    if ctx.dialect == "snowflake":
        return f"{column} COLLATE {collation}"
    if ctx.dialect in ("mysql", "mariadb"):
        return column
    return f"lower({column})"


@sql_macro(optional=True)
def between(
    column: Sql,
    start: Param,
    end: Param,
    bounds: Literal["'[]'", "'[)'"] = "'[]'",
) -> str:
    """Keep a column within a range whose ends may each be missing.

    ```sql
    AND tpl.between(ir.date, :date_from, :date_to)
    AND tpl.between(created_at, :since, :until, '[)')
    ```

    `BETWEEN` when both ends are there, `>=` or `<=` when one is, `TRUE` when
    neither is: an end holds no value as in `if_set`. ``bounds`` is `'[]'`,
    which includes the end as `BETWEEN` does, or `'[)'`, which leaves it out,
    as a range of times wants. A start after the end is data, and matches
    nothing.
    """
    has_start, has_end = _is_set(start.value), _is_set(end.value)
    closed = bounds == "'[]'"
    below = "<=" if closed else "<"
    if has_start and has_end:
        if closed:
            return f"{column} BETWEEN {start} AND {end}"
        return f"({column} >= {start} AND {column} < {end})"
    if has_start:
        return f"{column} >= {start}"
    if has_end:
        return f"{column} {below} {end}"
    return "TRUE"


@sql_macro(name="values")
def values_table(ctx: Context, rows: Param) -> str:
    """Write a small table out in the query, one parameter per value.

    ```sql
    SELECT column1 AS position, column2 AS name FROM tpl.values(:segments) AS v
    ```

    Rows are tuples or lists of one length; a plain value is a row of one
    column. The columns are `column1`, `column2`, ... on every database: MySQL
    and MariaDB name the columns of `VALUES` otherwise, so there it is written
    as `SELECT ... UNION ALL SELECT ...`. Name them in the select list, since
    MariaDB takes no `AS v (position, name)`. The database works out the types,
    and PostgreSQL wants one type down a column: cast where the rows mix them.

    For a few dozen rows. Hundreds are better sent as one JSON value and
    unpacked in the database, with `FLATTEN` or `json_array_elements`.

    Raises:
        MacroArgumentError: if there are no rows, or they differ in length.

    """
    table = [
        tuple(row) if isinstance(row, tuple | list) else (row,)
        for row in rows.value or ()
    ]
    if not table:
        problem = f"`:{rows.name}` has no rows, and `VALUES` without one is not SQL"
        raise MacroArgumentError(values_table.name, problem)
    width = len(table[0])
    for number, row in enumerate(table, 1):
        if len(row) != width:
            problem = (
                f"row {number} of `:{rows.name}` has {len(row)} values, "
                f"and row 1 has {width}"
            )
            raise MacroArgumentError(values_table.name, problem)
    name = _named(rows)
    bound = [[ctx.bind(value, name) for value in row] for row in table]
    if ctx.dialect in ("mysql", "mariadb"):
        first, *rest = bound
        named = ", ".join(
            f"{value} AS column{index}" for index, value in enumerate(first, 1)
        )
        selects = [f"SELECT {named}", *(f"SELECT {', '.join(row)}" for row in rest)]
        return f"({' UNION ALL '.join(selects)})"
    written = ", ".join(f"({', '.join(row)})" for row in bound)
    return f"(VALUES {written})"


@sql_macro
def json_object(ctx: Context, *pairs: Sql) -> str:
    """Build a JSON object of keys and values: `json_object('id', id, 'name', name)`.

    `JSON_BUILD_OBJECT` on PostgreSQL, `OBJECT_CONSTRUCT` on Snowflake, and
    `JSON_OBJECT` on MySQL, MariaDB and SQLite.

    Raises:
        MacroArgumentError: on another database.

    """
    name = _for_dialect(
        ctx,
        json_object.name,
        postgresql="JSON_BUILD_OBJECT",
        snowflake="OBJECT_CONSTRUCT",
        mysql="JSON_OBJECT",
        mariadb="JSON_OBJECT",
        sqlite="json_object",
    )
    return f"{name}({', '.join(pairs)})"


@sql_macro
def array_agg(ctx: Context, value: Sql, *order_by: Sql) -> str:
    """Gather the values of a group into an array, in the order the rest name.

    `ARRAY_AGG(value ORDER BY ...)` on PostgreSQL, and `ARRAY_AGG(value)
    WITHIN GROUP (ORDER BY ...)` on Snowflake.

    Raises:
        MacroArgumentError: on another database, which has no arrays.

    """
    _for_dialect(ctx, array_agg.name, postgresql=True, snowflake=True)
    return _aggregate(ctx, "ARRAY_AGG", [value], order_by)


@sql_macro
def string_agg(ctx: Context, value: Sql, separator: Sql, *order_by: Sql) -> str:
    """Join the values of a group with the separator, in the order the rest name.

    `STRING_AGG` on PostgreSQL, `LISTAGG ... WITHIN GROUP` on Snowflake,
    `GROUP_CONCAT ... SEPARATOR` on MySQL and MariaDB, and `group_concat` on
    SQLite, which takes an order from 3.44 on.

    Raises:
        MacroArgumentError: on another database.

    """
    _for_dialect(
        ctx,
        string_agg.name,
        postgresql=True,
        snowflake=True,
        mysql=True,
        mariadb=True,
        sqlite=True,
    )
    order = f" ORDER BY {', '.join(order_by)}" if order_by else ""
    if ctx.dialect in ("mysql", "mariadb"):
        return f"GROUP_CONCAT({value}{order} SEPARATOR {separator})"
    if ctx.dialect == "snowflake":
        return _aggregate(ctx, "LISTAGG", [value, separator], order_by)
    name = "group_concat" if ctx.dialect == "sqlite" else "STRING_AGG"
    return f"{name}({value}, {separator}{order})"


def _aggregate(
    ctx: Context, name: str, args: Sequence[str], order_by: Sequence[str]
) -> str:
    """Write an ordered aggregate: the order inside it, or `WITHIN GROUP` after."""
    written = ", ".join(args)
    if not order_by:
        return f"{name}({written})"
    order = f"ORDER BY {', '.join(order_by)}"
    if ctx.dialect == "snowflake":
        return f"{name}({written}) WITHIN GROUP ({order})"
    return f"{name}({written} {order})"


@sql_macro
def array_contains(ctx: Context, array: Sql, value: Sql) -> str:
    """Whether an array holds the value.

    `value = ANY(array)` on PostgreSQL, and `ARRAY_CONTAINS(value::variant,
    array)` on Snowflake.

    Raises:
        MacroArgumentError: on another database, which has no arrays.

    """
    _for_dialect(ctx, array_contains.name, postgresql=True, snowflake=True)
    if ctx.dialect == "snowflake":
        return f"ARRAY_CONTAINS({value}::variant, {array})"
    return f"{value} = ANY({array})"


@sql_macro
def on_dialect(ctx: Context, branch: Sql, *branches: Sql) -> str:
    """Write the SQL of the database in hand: `postgresql = a, snowflake = b`.

    ```sql
    FROM tpl.on_dialect(postgresql = dim_country, snowflake = facts.prod.dim_country)
    ```

    `default = ...` is for every database not named. A way out rather than a
    first choice: a macro that names the difference, as `icontains` does, says
    more, and this is for what nothing names, such as a table that lives
    elsewhere.

    Raises:
        MacroArgumentError: if a branch is not `name = sql`, or none is for the
            database in hand.

    """
    written: dict[str, str] = {}
    for one in (branch, *branches):
        named = _COLUMN_AS.fullmatch(one)
        if named is None:
            problem = f"takes `dialect = sql` branches, got {one.strip()!r}"
            raise MacroArgumentError(on_dialect.name, problem)
        written[named.group(1)] = named.group(2)
    if ctx.dialect in written:
        return written[ctx.dialect]
    if "default" in written:
        return written["default"]
    problem = (
        f"has no branch for {ctx.dialect}, and no `default = ...`: "
        f"it has {', '.join(written)}"
    )
    raise MacroArgumentError(on_dialect.name, problem)


def _for_dialect(ctx: Context, macro: str, **forms: Any) -> Any:  # noqa: ANN401
    """Return what the database in hand writes, or say the macro has no form for it.

    Raises:
        MacroArgumentError: if the dialect is none of them.

    """
    if ctx.dialect not in forms:
        problem = f"has no form for {ctx.dialect}: it writes {', '.join(forms)}"
        raise MacroArgumentError(macro, problem)
    return forms[ctx.dialect]


@sql_macro
def identifier(ctx: Context, name: Param, *allowed: Sql) -> str:
    """Write a name from a parameter, quoted the way this database quotes one.

    The Jinja `identifier` filter does the same, and writes the same SQL: a
    name that needs no quoting is left alone. A tuple or a list is a qualified
    name, `("reports", "events")` for `reports.events`.

    With names listed after the parameter, only those are taken, matched the way
    `order_by` matches a sort string, and written as the template lists them:

    ```sql
    SELECT tpl.identifier(:column, id, name, fans = fans_count) FROM artists
    ```

    A name that comes from a request wants the list: quoting keeps SQL out, but
    not a column the table does not have.
    """
    value = name.value
    if allowed:
        offered = _offered(allowed)
        if not isinstance(value, str) or not value:
            raise UnknownIdentifierError(value, offered)
        try:
            return offered[_field_named(value, offered)]
        except UnknownOrderFieldError:
            raise UnknownIdentifierError(value, offered) from None
    parts = (value,) if isinstance(value, str) else tuple(value or ())
    if not parts or not all(isinstance(part, str) and part for part in parts):
        raise UnknownIdentifierError(value)
    return ".".join(ctx.quote(part) for part in parts)


@sql_macro
def each(ctx: Context, values: Param) -> str:
    """Each value of a list as a parameter of its own, for `IN (...)`.

    The Jinja `inclause` filter does the same. The parentheses are the
    template's, so the SQL stays SQL: `WHERE team IN (tpl.each(:teams))`.
    `IN :teams` binds the list as one expanding parameter instead, and a
    template usually wants that.

    Raises:
        MacroArgumentError: if the list is empty: `IN ()` is not SQL.

    """
    items = list(values.value or ())
    if not items:
        problem = f"`:{values.name}` is empty, and `IN ()` is not SQL"
        raise MacroArgumentError(each.name, problem)
    return ", ".join(ctx.bind(item, _named(values)) for item in items)


BUILTIN_MACROS: Mapping[str, Macro] = {
    macro.name: macro
    for macro in (
        if_set,
        unless_set,
        order_by,
        icontains,
        icollate,
        json_object,
        array_agg,
        string_agg,
        array_contains,
        on_dialect,
        between,
        identifier,
        each,
        values_table,
    )
}

tpl = SimpleNamespace(**BUILTIN_MACROS)
"""The built-in macros, to call from a macro of your own as a template would.

```python
@sql_macro
def search(q: Param, *columns: Sql) -> str:
    if not q.value:
        return "TRUE"
    return " OR ".join(tpl.icontains(column, q) for column in columns)
```
"""


class Filter:
    """A template filter, registered the way jinja2sql registers one.

    ```python
    Templates("app/sql", filters={"in_span": Filter(in_span, bind=True)})
    ```

    ``bind=True`` calls the filter with a jinja2sql `Binder` as its first
    argument, so a filter writing SQL of its own binds the values through it:

    ```python
    def in_span(binder, span):
        start, end = span
        return binder.raw(
            f"BETWEEN {binder.bind('span', start)} AND {binder.bind('span', end)}"
        )
    ```

    A plain function needs none of this and goes in as it is: whatever it
    returns is bound as one more value of the statement.
    """

    __slots__ = ("bind", "func")

    def __init__(self, func: Callable[..., Any], *, bind: bool = False) -> None:
        self.func = func
        self.bind = bind

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.func!r}, bind={self.bind})"


class JinjaEngine:
    """Renders Jinja templates through jinja2sql.

    Everything Jinja lives here, so that moving off it is deleting this class.
    """

    def __init__(
        self,
        paths: Sequence[PathLike],
        *,
        auto_reload: bool,
        filters: Mapping[str, Callable[..., Any] | Filter],
        globals: Mapping[str, Any],  # noqa: A002
    ) -> None:
        self.paths = paths
        self.auto_reload = auto_reload
        self.filters = filters
        self.globals = globals

    @cached_property
    def renderer(self) -> Jinja2SQL:
        """The Jinja environment behind this, built on first use.

        Raises:
            MissingDependencyError: if the extra is not installed.

        """
        jinja2sql = _required(Jinja2SQL, "jinja2sql", "SQL templates", "sqlakit[sql]")
        environment = jinja2.Environment(
            loader=_jinja_loader([str(path) for path in self.paths]),
            auto_reload=self.auto_reload,
            autoescape=True,
        )
        environment.globals.update(self.globals)
        # Named parameters: `text()` reads `:name` and nothing else.
        renderer = jinja2sql(environment, param_style=_placeholder)
        # Ours quotes through the dialect's preparer, jinja2sql's through one char.
        renderer.register_filter("identifier", _identifier)
        for name, filter_ in self.filters.items():
            if isinstance(filter_, Filter):
                renderer.register_filter(name, filter_.func, bind=filter_.bind)
            else:
                renderer.register_filter(name, filter_)
        return renderer

    def render_file(
        self,
        name: str,
        context: Mapping[str, Any],
        preparer: Any,  # noqa: ANN401
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template file, and the values to bind to it."""
        token = _preparer.set(preparer)
        try:
            sql, params = self.renderer.from_file(name, context=context)
        except jinja2.TemplateNotFound as error:
            if error.name != name:
                raise  # what the template includes, said as Jinja says it
            raise TemplateNotFoundError(name, self.paths) from None
        finally:
            _preparer.reset(token)
        # Named parameters come back as a mapping, positional ones as a sequence.
        return sql, cast("Mapping[str, Any]", params)

    def render_string(
        self,
        source: str,
        context: Mapping[str, Any],
        preparer: Any,  # noqa: ANN401
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template written out, and the values to bind to it."""
        token = _preparer.set(preparer)
        try:
            sql, params = self.renderer.from_string(source, context=context)
        finally:
            _preparer.reset(token)
        return sql, cast("Mapping[str, Any]", params)

    def check(self, names: Iterable[str]) -> None:
        """Compile these templates.

        Raises:
            jinja2.TemplateSyntaxError: naming the file and the line.

        """
        environment = self.renderer.env
        for name in names:
            environment.get_template(name)


class Templates:
    """The directory a database's SQL templates live in, and how they render.

    A path is enough; the object is for the rest:

    ```python
    db = Database(DB_URL, templates=Templates("app/sql", auto_reload=DEBUG))
    ```

    ``auto_reload`` reads a template again when its file changes, which a
    development server wants and a production one does not. ``filters`` and
    ``globals`` are handed to the Jinja environment, and are refused if they have
    to be awaited: rendering makes a string, in both APIs.

    A filter is a plain function, whose return value is bound as one more value
    of the statement. `Filter(func, bind=True)` registers one that writes SQL of
    its own instead, and is handed a binder for the values inside it.

    A file named `*.tpl.sql` is not Jinja: it is SQL with `:name` parameters and
    `tpl.<macro>(...)` calls. ``macros`` are the ones an application adds to the
    built-in ones, which `sqlakit macros` lists:

    ```python
    Templates("app/sql", macros=[for_accounts])
    Templates("app/sql", macros=["app.sql.macros"])  # every macro of a module
    ```

    ``namespace`` is the schema name calls are written under, `tpl` unless a
    real schema has that name: `Templates("app/sql", namespace="q")` reads
    `q.if_set(...)`.

    ``engine`` says what renders everything else, a `.sql` file and
    `db.sql.from_string(...)`: `jinja`, or `tpl` once no template needs Jinja,
    which then is never imported.
    """

    def __init__(  # noqa: PLR0913 - the options of one object
        self,
        path: PathLike | Sequence[PathLike] = (),
        *,
        auto_reload: bool = False,
        filters: Mapping[str, Callable[..., Any] | Filter] | None = None,
        globals: Mapping[str, Any] | None = None,  # noqa: A002
        macros: Iterable[Macro | str] = (),
        engine: Literal["jinja", "tpl"] = "jinja",
        namespace: str = NAMESPACE,
    ) -> None:
        self.paths = (
            (path,) if isinstance(path, str | Path) else tuple(path)  # type: ignore[misc]
        )
        self.auto_reload = auto_reload
        self.filters = dict(filters or {})
        self.globals = dict(globals or {})
        self.macros = registered(macros)
        if engine not in ("jinja", "tpl"):
            msg = f"`engine` is `jinja` or `tpl`, not {engine!r}"
            raise ValueError(msg)
        self.engine = engine
        if not _IDENTIFIER.fullmatch(namespace):
            msg = f"`namespace` is a plain name, such as `tpl`, not {namespace!r}"
            raise ValueError(msg)
        self.namespace = namespace
        for name, value in (*self.filters.items(), *self.globals.items()):
            called = value.func if isinstance(value, Filter) else value
            if iscoroutinefunction(called):
                raise AsyncFilterError(name)

    def __repr__(self) -> str:
        paths = ", ".join(str(path) for path in self.paths)
        return f"{type(self).__name__}({paths!r})"

    @cached_property
    def macro_engine(self) -> MacroEngine:
        """The engine that renders templates with `tpl.` macros."""
        return MacroEngine(
            self.paths,
            self.macros,
            auto_reload=self.auto_reload,
            namespace=self.namespace,
            every_file=self.engine == "tpl",
        )

    @cached_property
    def jinja_engine(self) -> JinjaEngine:
        """The engine that renders Jinja templates."""
        return JinjaEngine(
            self.paths,
            auto_reload=self.auto_reload,
            filters=self.filters,
            globals=self.globals,
        )

    @property
    def renderer(self) -> Jinja2SQL:
        """The Jinja environment behind the Jinja templates."""
        return self.jinja_engine.renderer

    def engine_for(self, name: str | None) -> MacroEngine | JinjaEngine:
        """Return what renders a template file, or a string when ``name`` is None."""
        if self.engine == "tpl" or (name is not None and name.endswith(SUFFIX)):
            return self.macro_engine
        return self.jinja_engine

    def render(
        self,
        source: str,
        context: Mapping[str, Any],
        *,
        preparer: Any,  # noqa: ANN401 - SQLAlchemy's IdentifierPreparer
        inline: bool = False,
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the SQL of a template, and the values to bind to it.

        Synchronous in both APIs: it reads a compiled template and builds a string.

        Raises:
            SQLNotConfiguredError: if a file is asked for and no path was given.
            TemplateNotFoundError: if no path holds that template.

        """
        if inline:
            return self.engine_for(None).render_string(source, context, preparer)
        if not self.paths:
            raise SQLNotConfiguredError
        return self.engine_for(source).render_file(source, context, preparer)

    def names(self) -> list[str]:
        """Return the name of every `.sql` file under the paths."""
        found = {
            path.relative_to(root).as_posix()
            for root in map(Path, self.paths)
            for path in root.rglob("*.sql")
        }
        return sorted(found)

    def check(self) -> None:
        """Compile every `.sql` template, so a broken one fails where deploys do.

        Raises:
            SQLNotConfiguredError: if there is nowhere to look, which makes checking
                a lie rather than a pass.
            jinja2.TemplateSyntaxError: naming the file and the line.
            MacroSyntaxError: if a macro template cannot be read.
            UnknownMacroError: if it calls a macro nobody registered.
            MacroArgumentError: if a call has arguments its macro cannot take.

        """
        if not self.paths:
            raise SQLNotConfiguredError
        by_engine: dict[MacroEngine | JinjaEngine, list[str]] = {}
        for name in self.names():
            by_engine.setdefault(self.engine_for(name), []).append(name)
        for engine, names in by_engine.items():
            engine.check(names)


class BaseSQLQuery(Generic[RowT, DatabaseT]):
    """The source of the SQL, its context, and the type its rows become.

    Built by `db.sql(...)`. Nothing can be narrowed: what the SQL selects is
    what comes back.
    """

    def __init__(  # noqa: PLR0913 - the shape of a query, not a call site
        self,
        db: DatabaseT,
        source: str | Executable,
        context: Mapping[str, Any],
        *,
        inline: bool = False,
        type_: type[Any] | None = None,
        scalar: bool = False,
        validation: Mapping[str, Any] | None = None,
    ) -> None:
        self.db = db
        self.source = source
        self.context = context
        self.inline = inline
        self.type = type_
        self.scalar = scalar
        self.validation = dict(validation or {})

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.source!r})"

    @cached_property
    def statement(self) -> Executable:
        """The SQL this runs, rendered and bound.

        A test asserts on it, and `EXPLAIN` takes it. A statement handed over ready
        is itself.
        """
        if not isinstance(self.source, str):
            return self.source
        dialect = self.db.engine.dialect
        context = {"dialect": dialect.name, **self.context}
        sql, params = templates_of(self.db).render(
            self.source,
            context,
            preparer=dialect.identifier_preparer,
            inline=self.inline,
        )
        return _statement(sql, params, label=None if self.inline else self.source)

    def __clause_element__(self) -> Executable:
        """Stand in for the statement wherever SQLAlchemy expects one.

        ```python
        User.query.from_statement(db.sql("users/active.sql", team="red")).all()
        ```
        """
        return self.statement

    def _as(self, query: type[QueryT], **changes: Any) -> QueryT:  # noqa: ANN401
        """Return the same template read another way, as another class.

        The classes are the API: what a query no longer offers, it no longer
        has, so `typed()` cannot be called on rows that carry a type already.
        """
        arguments = {
            "inline": self.inline,
            "type_": self.type,
            "scalar": self.scalar,
            "validation": self.validation,
            **changes,
        }
        return query(self.db, self.source, self.context, **arguments)

    def _shaped(self, rows: Sequence[Any]) -> Sequence[Any]:
        if self.type is None:
            return rows
        adapter = _adapter(self.type)
        return [
            adapter.validate_python(_as_python(row, self.type), **self.validation)
            for row in rows
        ]

    def _shaped_one(self, row: Any) -> Any:  # noqa: ANN401
        if self.type is None or row is None:
            return row
        return _adapter(self.type).validate_python(
            _as_python(row, self.type), **self.validation
        )

    def _executable(self, *, size: int | None = None) -> Executable:
        if size is None:
            return self.statement
        return self.statement.execution_options(yield_per=size)


def _jinja_loader(paths: list[str]) -> jinja2.BaseLoader:
    """Return a loader of the Jinja templates under the paths, and of no others.

    `{% include %}` of a `.tpl.sql` file would put its `tpl.` calls in the SQL
    unexpanded, so it is refused.
    """

    class Loader(jinja2.FileSystemLoader):
        def get_source(
            self, environment: jinja2.Environment, template: str
        ) -> tuple[str, str, Callable[[], bool]]:
            if template.endswith(SUFFIX):
                message = (
                    f"`{template}` is a macro template, which Jinja cannot include: "
                    f"turn this one into a `{SUFFIX}` template, and include it with "
                    f"`tpl.include('{template}')`"
                )
                raise jinja2.TemplateNotFound(template, message)
            return super().get_source(environment, template)

    return Loader(paths)


def _identifier(value: Any) -> Markup:  # noqa: ANN401
    """Return a name quoted the way the database in hand quotes one.

    The preparer decides both the quoting character and whether a name needs
    quoting at all: `name` is left alone on Oracle, where a quoted lowercase
    name is a different, non-existent column.
    """
    parts = (value,) if isinstance(value, str) else value
    preparer = _preparer.get()
    # The preparer escapes what it quotes; nothing here reaches the SQL raw.
    return Markup(".".join(preparer.quote(str(part)) for part in parts))


def _placeholder(name: str, index: int) -> str:  # noqa: ARG001 - the style's shape
    """Return the placeholder a value renders as.

    A space follows it so that a cast can: `{{ id }}::uuid` renders `:id__1
    ::uuid`, and `text()` reads the parameter and leaves the cast alone. Without
    the space it reads `:id__` and the statement never runs.
    """
    return f":{name} "


def require_pydantic() -> None:
    """Raise unless pydantic is installed, which `typed()` validates rows with.

    Raises:
        MissingDependencyError: if it is not.

    """
    _required(TypeAdapter, "pydantic", "`typed()`")


def templates_of(db: BaseDatabase[Any, Any]) -> Templates:
    """Return the templates of this database, made once and kept on it."""
    templates = db.templates
    if not isinstance(templates, Templates):
        templates = Templates() if templates is None else Templates(templates)
        db.templates = templates
    return templates


def _required(
    module: Any,  # noqa: ANN401
    package: str,
    needed_by: str,
    install: str | None = None,
) -> Any:  # noqa: ANN401
    """Return it, or say what to install.

    Raises:
        MissingDependencyError: if the import failed.

    """
    if module is None:
        raise MissingDependencyError(package, needed_by, install)
    return module


def _statement(
    sql: str,
    params: Mapping[str, Any],
    *,
    label: str | None,
) -> sa.TextClause:
    """Return the SQL as a statement, with every value bound to it.

    The template's name goes in as a comment, so a slow query log and `Recording`
    say which file the SQL came from.

    Raises:
        StrayParameterError: if the SQL holds something SQLAlchemy reads as a
            parameter that nothing binds, such as a colon inside a JSON literal.

    """
    if label is not None:
        # `*/` in a name would end the comment early and leak into the SQL.
        sql = f"/* {label.replace('*/', '* /')} */\n{sql}"
    clause, named = _text(sql)
    stray = named - set(params)
    if stray:
        raise StrayParameterError(sorted(stray), label)
    # A macro template hands over its whole context, and binds what the SQL names.
    return clause.bindparams(
        *(_bound(name, value) for name, value in params.items() if name in named)
    )


_POSIX_CLASS = re.compile(
    r"\[:(alnum|alpha|blank|cntrl|digit|graph|lower|print|punct|space|upper|word|xdigit):\]"
)
"""A class in a regular expression, `[:punct:]`, which `text()` reads as `:punct`."""


@lru_cache(maxsize=1024)
def _text(sql: str) -> tuple[sa.TextClause, frozenset[str]]:
    """Return the SQL as `text()`, and the names of the parameters it holds.

    Reading the parameters out of the SQL is most of what building a statement
    costs, and a template renders the same SQL whenever its values have the same
    shape. Sharing the clause is safe: `bindparams` returns a copy.
    """
    clause = sa.text(_POSIX_CLASS.sub(r"[\\:\1\\:]", sql))
    named = frozenset(
        element.key
        for element in clause.get_children()
        if isinstance(element, sa.BindParameter)
    )
    return clause, named


def _bound(name: str, value: Any) -> sa.BindParameter[Any]:  # noqa: ANN401
    """Return the parameter to bind, as the value it holds asks to be bound.

    A sequence becomes an expanding parameter, so `IN :ids` is a list rather
    than a syntax error. A `bindparam` of your own carries its type through,
    which is how a value the driver cannot type is spelled out.
    """
    if isinstance(value, sa.BindParameter):
        return sa.bindparam(
            name,
            _expandable(value.value),
            type_=value.type,
            expanding=value.expanding or _expands(value.value),
        )
    return sa.bindparam(name, _expandable(value), expanding=_expands(value))


def _expands(value: Any) -> bool:  # noqa: ANN401
    """Whether this is many values rather than one."""
    return isinstance(value, list | tuple | set | frozenset)


def _expandable(value: Any) -> Any:  # noqa: ANN401
    """Return it as the list an expanding parameter takes."""
    return list(value) if _expands(value) else value


def _as_python(row: Any, type_: Any) -> Any:  # noqa: ANN401
    """Return what pydantic validates: the whole row, or one column of it.

    The type decides. One that reads a mapping takes the row as a mapping;
    anything else takes the first column's value, so `SELECT count(*)` reads
    as an `int` and a JSON column reads as what it holds.
    """
    mapping = getattr(row, "_mapping", None)
    if mapping is None:
        return row
    if _reads_a_row(type_):
        return dict(mapping)
    return next(iter(mapping.values()), None)


@cache
def _reads_a_row(type_: Any) -> bool:  # noqa: ANN401
    """Whether this type is built from a row's columns rather than one value."""
    origin = get_origin(type_) or type_
    if is_typeddict(type_):
        return True
    if not isinstance(origin, type):
        return False
    if BaseModel is not None and issubclass(origin, BaseModel):
        return True
    if is_dataclass(origin):
        return True
    if issubclass(origin, tuple) and hasattr(origin, "_fields"):  # a NamedTuple
        return True
    return issubclass(origin, Mapping)


@cache
def _adapter(type_: Any) -> TypeAdapter[Any]:  # noqa: ANN401
    """Return the adapter for this type, built once for the process."""
    return TypeAdapter(type_)
