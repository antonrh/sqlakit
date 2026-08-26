from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import is_dataclass
from functools import cache, cached_property
from inspect import iscoroutinefunction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast, get_origin, is_typeddict

import sqlalchemy as sa
from markupsafe import Markup

from .exceptions import (
    AsyncFilterError,
    MissingDependencyError,
    SQLNotConfiguredError,
    StrayParameterError,
    TemplateNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import jinja2
    from jinja2sql import Jinja2SQL
    from sqlalchemy.sql import Executable

    from ._base import BaseDatabase
else:
    try:
        import jinja2
        from jinja2sql import Jinja2SQL
    except ImportError:  # pragma: no cover - the extra is installed in CI
        jinja2 = Jinja2SQL = None

if TYPE_CHECKING:
    from pydantic import BaseModel, TypeAdapter
else:
    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:  # pragma: no cover - pydantic is installed in CI
        BaseModel = TypeAdapter = None

__all__ = ["BaseSQLQuery", "Templates", "require_pydantic", "templates_of"]

_preparer: ContextVar[Any] = ContextVar("sqlakit.identifier_preparer")
"""The preparer of the database a template is rendering for."""

RowT = TypeVar("RowT")
OtherT = TypeVar("OtherT")
SessionT = TypeVar("SessionT")
DatabaseT = TypeVar("DatabaseT", bound="BaseDatabase[Any, Any]")
QueryT = TypeVar("QueryT", bound="BaseSQLQuery[Any, Any]")

PathLike = str | Path
"""Where templates are looked for: one directory, or several."""


class Templates:
    """Where a database's SQL templates live, and how they are rendered.

    A path is enough; the object is for the rest:

    ```python
    db = Database(DB_URL, templates=Templates("app/sql", auto_reload=DEBUG))
    ```

    ``auto_reload`` reads a template again when its file changes, which a
    development server wants and a production one does not. ``filters`` and
    ``globals`` are handed to the Jinja environment, and are refused if they have
    to be awaited: rendering makes a string, in both APIs.
    """

    def __init__(
        self,
        path: PathLike | Sequence[PathLike] = (),
        *,
        auto_reload: bool = False,
        filters: Mapping[str, Callable[..., Any]] | None = None,
        globals: Mapping[str, Any] | None = None,  # noqa: A002
    ) -> None:
        self.paths = (
            (path,) if isinstance(path, str | Path) else tuple(path)  # type: ignore[misc]
        )
        self.auto_reload = auto_reload
        self.filters = dict(filters or {})
        self.globals = dict(globals or {})
        for name, value in (*self.filters.items(), *self.globals.items()):
            if iscoroutinefunction(value):
                raise AsyncFilterError(name)

    def __repr__(self) -> str:
        paths = ", ".join(str(path) for path in self.paths)
        return f"{type(self).__name__}({paths!r})"

    @cached_property
    def renderer(self) -> Jinja2SQL:
        """The Jinja environment behind this, built on first use.

        Raises:
            MissingDependencyError: if the extra is not installed.

        """
        jinja2sql = _required(Jinja2SQL, "jinja2sql", "SQL templates", "sqlakit[sql]")
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader([str(path) for path in self.paths]),
            auto_reload=self.auto_reload,
            autoescape=True,
        )
        environment.globals.update(self.globals)
        # Always named parameters: what comes back is handed to `text()`,
        # which reads `:name` and nothing else. The driver's own style is
        # SQLAlchemy's business, and a template that picked one would be wrong
        # on the next database.
        renderer = jinja2sql(environment, param_style=_placeholder)
        # Ours rather than jinja2sql's: the preparer of the database in hand
        # knows both how it quotes and when it has to, which is the difference
        # between `"name"` and `name` on Oracle.
        renderer.register_filter("identifier", _identifier)
        for name, filter_ in self.filters.items():
            renderer.register_filter(name, filter_)
        return renderer

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
        token = _preparer.set(preparer)
        try:
            if inline:
                sql, params = self.renderer.from_string(source, context=context)
            else:
                if not self.paths:
                    raise SQLNotConfiguredError
                try:
                    sql, params = self.renderer.from_file(source, context=context)
                except jinja2.TemplateNotFound:
                    raise TemplateNotFoundError(source, self.paths) from None
        finally:
            _preparer.reset(token)
        # Always a mapping: the parameters are named, and only a positional
        # style would hand back a sequence.
        return sql, cast("Mapping[str, Any]", params)

    def check(self) -> None:
        """Compile every `.sql` template, so a broken one fails where deploys do.

        Raises:
            SQLNotConfiguredError: if there is nowhere to look, which makes checking
                a lie rather than a pass.
            jinja2.TemplateSyntaxError: naming the file and the line.

        """
        if not self.paths:
            raise SQLNotConfiguredError
        environment = self.renderer.env
        for name in environment.list_templates(extensions=("sql",)):
            environment.get_template(name)


class BaseSQLQuery(Generic[RowT, DatabaseT]):
    """Where the SQL comes from, its context, and what its rows become.

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
    ) -> None:
        self.db = db
        self.source = source
        self.context = context
        self.inline = inline
        self.type = type_
        self.scalar = scalar

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
            **changes,
        }
        return query(self.db, self.source, self.context, **arguments)

    def _shaped(self, rows: Sequence[Any]) -> Sequence[Any]:
        if self.type is None:
            return rows
        adapter = _adapter(self.type)
        return [adapter.validate_python(_as_python(row, self.type)) for row in rows]

    def _shaped_one(self, row: Any) -> Any:  # noqa: ANN401
        if self.type is None or row is None:
            return row
        return _adapter(self.type).validate_python(_as_python(row, self.type))

    def _executable(self, *, size: int | None = None) -> Executable:
        if size is None:
            return self.statement
        return self.statement.execution_options(yield_per=size)


def _identifier(value: Any) -> Markup:  # noqa: ANN401
    """Return a name quoted the way the database in hand quotes one.

    The preparer decides both the quoting character and whether a name needs
    quoting at all: `name` is left alone on Oracle, where a quoted lowercase
    name is a different, non-existent column.
    """
    parts = (value,) if isinstance(value, str) else value
    preparer = _preparer.get()
    # The preparer escapes what it quotes; nothing here reaches the SQL raw.
    return Markup(".".join(preparer.quote(str(part)) for part in parts))  # noqa: S704


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
    clause = sa.text(sql)
    named = {
        element.key
        for element in clause.get_children()
        if isinstance(element, sa.BindParameter)
    }
    stray = named - set(params)
    if stray:
        raise StrayParameterError(sorted(stray), label)
    return clause.bindparams(*(_bound(name, value) for name, value in params.items()))


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
