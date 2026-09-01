from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    NamedTuple,
    Protocol,
    Self,
    TypeAlias,
    cast,
)

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import (
    InstrumentedAttribute,
    contains_eager,
    joinedload,
    selectinload,
    subqueryload,
)
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from sqlalchemy.sql import operators

# `TypeVar` from here, for the default that keeps `Page[User]` the counted one.
from typing_extensions import TypeVar

from ._model import resolve_alias, soft_delete_column
from .exceptions import (
    BulkQueryError,
    ConflictingJoinError,
    InstanceNotFoundError,
    InvalidCursorError,
    InvalidNullsError,
    InvalidOrderFieldError,
    KeyLookupError,
    MultipleInstancesFoundError,
    NullCursorValueError,
    PageItemsMismatchError,
    RawStatementError,
    UncomparableOrderingError,
    UnknownOrderFieldError,
    UnorderedPageError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

    from sqlalchemy.sql._typing import _ColumnExpressionArgument
    from sqlalchemy.sql.roles import ReturnsRowsRole
    from sqlalchemy.sql.selectable import ForUpdateParameter

__all__ = [
    "CASE_INSENSITIVE_COLLATIONS",
    "BaseQuery",
    "CaseInsensitive",
    "CursorPage",
    "NullsPlacement",
    "OrderBy",
    "Page",
    "UncountedPage",
    "one_row",
    "one_row_or_none",
    "orderable",
    "orderable_columns",
    "ordered",
]

CASE_INSENSITIVE_COLLATIONS: dict[str, str] = {"sqlite": "NOCASE"}
"""The collation `ignore_case` orders by, per dialect.

A dialect with no entry orders by `lower(...)`, which every database has.
Name the collation you created, once, before any query runs:

```python
sqlakit.CASE_INSENSITIVE_COLLATIONS["postgresql"] = "und-ci-ai"
```

`lower()` folds the case and leaves the accents, so it orders differently
from a collation like `und-ci-ai`, and it cannot read an index built on the
column. A column that already carries a case-insensitive collation needs no
`ignore_case` at all: name the same collation here, or leave it off.

A collation decides the whole order, the alphabet and the accents along with
the case. This one is asked for only by `ignore_case`. To sort by another,
name it on the column: `User.name.collate("de-DE")`.
"""

HIDDEN = "hidden"
"""The rows a soft delete marked are left out, as a read does by default."""

INCLUDED = "included"
"""They are read alongside the rest."""

ONLY = "only"
"""They are the only rows read."""

ModelT = TypeVar("ModelT")
OtherT = TypeVar("OtherT")
TotalT = TypeVar("TotalT", bound="int | None", default=int)
"""What a page knows about its total: an `int`, or `None` when nobody counted."""
RowT = TypeVar("RowT")
RowT_co = TypeVar("RowT_co", covariant=True)


class CaseInsensitive(sa.ColumnElement[Any]):
    """A column compared without regard to case, however the dialect does it.

    The dialect is the one the query runs on, not the one it was built against,
    so a model ordered this way works on `SQLite` under test and on the server
    it ships to.
    """

    inherit_cache = True

    def __init__(self, element: sa.ColumnElement[Any]) -> None:
        self.element = element
        self.type = element.type


@compiles(CaseInsensitive)
def _compile_case_insensitive(
    element: CaseInsensitive,
    compiler: Any,  # noqa: ANN401
    **kw: Any,  # noqa: ANN401
) -> str:
    collation = CASE_INSENSITIVE_COLLATIONS.get(compiler.dialect.name)
    if collation is None:
        return compiler.process(sa.func.lower(element.element), **kw)
    return compiler.process(sa.collate(element.element, collation), **kw)


class NullsPlacement(sa.ColumnElement[Any]):
    """An ordering clause that says where the rows with no value go.

    `MySQL` and `MariaDB` have no `NULLS FIRST` or `NULLS LAST`, so there the
    clause comes out as the two the standard is short for: whether the value is
    null, and then the ordering itself.
    """

    inherit_cache = True

    def __init__(self, clause: Any, *, last: bool) -> None:  # noqa: ANN401
        self.clause = clause
        self.last = last
        self.type = sa.Boolean()


@compiles(NullsPlacement)
def _compile_nulls(
    element: NullsPlacement,
    compiler: Any,  # noqa: ANN401
    **kw: Any,  # noqa: ANN401
) -> str:
    placed = sa.nulls_last if element.last else sa.nulls_first
    return compiler.process(placed(element.clause), **kw)


@compiles(NullsPlacement, "mysql")
def _compile_nulls_for_mysql(
    element: NullsPlacement,
    compiler: Any,  # noqa: ANN401
    **kw: Any,  # noqa: ANN401
) -> str:
    column, _ = _direction(element.clause)
    empty = column.is_(None)
    first = compiler.process(empty.asc() if element.last else empty.desc(), **kw)
    return f"{first}, {compiler.process(element.clause, **kw)}"


@dataclass(frozen=True, slots=True)
class Page(Generic[ModelT, TotalT]):
    """One page of rows, and how many there are in total.

    ``Page[User]`` is the counted page, whose ``total`` is an `int`.
    ``page(total=False)`` returns a `Page[User, None]` instead, so the code
    reading it is not asked about a total nobody counted.
    """

    items: Sequence[ModelT]
    total: TotalT
    limit: int
    offset: int
    has_next: bool = False

    def __post_init__(self) -> None:
        if self.total is not None:
            object.__setattr__(
                self, "has_next", self.offset + len(self.items) < self.total
            )

    def map(self, transform: Callable[[ModelT], OtherT]) -> Page[OtherT, TotalT]:
        """Return the page with every row put through ``transform``.

        ```python
        page = User.query.page(limit=20).map(UserResponse.from_user)
        ```
        """
        return self.with_items([transform(item) for item in self.items])

    def map_all(
        self,
        transform: Callable[[Sequence[ModelT]], Sequence[OtherT]],
    ) -> Page[OtherT, TotalT]:
        """Return the page with the rows put through ``transform`` together.

        For work that reads better in one go than row by row: one query for
        what the rows refer to, one call to a serializer that takes a list.
        """
        return self.with_items(transform(self.items))

    def with_items(self, items: Sequence[OtherT]) -> Page[OtherT, TotalT]:
        """Return the page carrying these rows instead, counts unchanged.

        What an asynchronous transform needs: `page.with_items(await serialize(...))`.

        Raises:
            PageItemsMismatchError: if there is not one item per row.

        """
        if len(items) != len(self.items):
            raise PageItemsMismatchError(len(self.items), len(items))
        return Page(
            items=items,
            total=self.total,
            limit=self.limit,
            offset=self.offset,
            has_next=self.has_next,
        )


UncountedPage: TypeAlias = Page[ModelT, None]
"""A page read with ``total=False``, which counted nothing.

The same class, named for what a signature means by it:

```python
def feed(page: UncountedPage[User]) -> Response: ...
```
"""


@dataclass(frozen=True, slots=True)
class CursorPage(Generic[ModelT]):
    """One page of rows, and the cursors that read the ones either side."""

    items: Sequence[ModelT]
    next_cursor: str | None = None
    previous_cursor: str | None = None

    @property
    def has_next(self) -> bool:
        """Whether a page follows this one."""
        return self.next_cursor is not None

    @property
    def has_previous(self) -> bool:
        """Whether a page comes before this one."""
        return self.previous_cursor is not None

    def map(self, transform: Callable[[ModelT], OtherT]) -> CursorPage[OtherT]:
        """Return the page with every row put through ``transform``."""
        return self.with_items([transform(item) for item in self.items])

    def map_all(
        self,
        transform: Callable[[Sequence[ModelT]], Sequence[OtherT]],
    ) -> CursorPage[OtherT]:
        """Return the page with the rows put through ``transform`` together."""
        return self.with_items(transform(self.items))

    def with_items(self, items: Sequence[OtherT]) -> CursorPage[OtherT]:
        """Return the page carrying these rows instead, cursors unchanged.

        Raises:
            PageItemsMismatchError: if there is not one item per row.

        """
        if len(items) != len(self.items):
            raise PageItemsMismatchError(len(self.items), len(items))
        return CursorPage(
            items=items,
            next_cursor=self.next_cursor,
            previous_cursor=self.previous_cursor,
        )


class OrderBy(NamedTuple):
    """A field to order by that a foreign key cannot reach on its own.

    A plain column of another table needs none of this: naming it in
    ``__orderable__`` joins its table on the key between them. This is for what
    that cannot answer, an alias, a subquery, or two paths to one table:

    ```python
    {"team": OrderBy(Team.name, join=cls.team)}
    ```

    Join what holds one row. A collection multiplies the rows, and a page of
    multiplied rows counts wrong: join a subquery that aggregates them instead,
    with the ``on`` it needs.

    The join is an outer one, so ordering by the field returns the rows with
    nothing on the other side as well. ``outer=False`` makes it an inner join,
    which drops them, and ``nulls`` on `order_by` says where they go.
    """

    expression: Any
    join: Any = None
    on: Any = None
    outer: bool = True


class SupportsClause(Protocol):
    """Anything SQLAlchemy will take a statement from, ours included."""

    def __clause_element__(self) -> Any: ...  # noqa: ANN401


class _Ordering(NamedTuple):
    """One column of a cursor's ordering: how to sort it, how to read it back."""

    column: sa.ColumnElement[Any]
    descending: bool
    attribute: str


class BaseQuery(Generic[ModelT]):
    """The statements a query builds, without running any of them.

    Every builder method returns a new query, so one can be kept around and
    branched from, the way a SQLAlchemy `Select` can.
    """

    def __init__(
        self,
        model: type[ModelT],
        db: Any,  # noqa: ANN401
        select: sa.Select[Any] | None = None,
    ) -> None:
        self.model = model
        self.db = db
        self._select = sa.select(model) if select is None else select
        self._statement: Any = None
        self.filtered = True
        self.deleted = HIDDEN
        # A page reads the same keyset ordering three times: for the statement
        # and for the cursor at either end.
        self._keyset_cache: tuple[list[_Ordering], str] | None = None

    def using(self, target: str | Any) -> Self:  # noqa: ANN401
        """Run this query on another database, named or handed over.

        ```python
        User.query.using("replica").order_by("name").page(limit=20)
        ```

        It pins the query: reads and writes both go where you said, whatever the
        routers would have answered.
        """
        db = resolve_alias(self.model, target) if isinstance(target, str) else target
        query = self._copy()
        query.db = db
        return query

    @property
    def select(self) -> sa.Select[Any]:
        """The SQLAlchemy `Select` this query has built so far.

        Read it for what the builders do not cover, and hand the result to
        `with_select`. Building a new query is how a query changes: this one
        cannot be assigned to.
        """
        return self._select

    def with_select(self, select: sa.Select[Any]) -> Self:
        """Return a query like this one, over the given select.

        This is what every builder is made of. Use it in a method of your own
        when the statement needs SQLAlchemy the builders do not cover.

        Raises:
            RawStatementError: if the query is a statement of its own, which
                has no select to replace.

        """
        self._reject_statement(
            "with_select",
            "there is no select to replace, so build the statement you want and "
            "hand it to `from_statement`",
        )
        query = self._copy()
        query._select = select  # noqa: SLF001 - a copy of this class
        query._keyset_cache = None  # noqa: SLF001 - the ordering may differ
        return query

    def _copy(self) -> Self:
        """Return this query again, statement and all, for a caller to adjust."""
        query = object.__new__(type(self))
        query.__dict__.update(self.__dict__)
        return query

    # --- Building ---

    @property
    def is_ordered(self) -> bool:
        """Whether this query carries an ordering.

        What a method of your own asks before it adds a default one, since
        `page` and `cursor_page` refuse a query with no ordering.
        """
        return bool(self._select._order_by_clauses)  # noqa: SLF001

    def unfiltered(self) -> Self:
        """Drop the model's own `__query_filter__` for this query.

        The rows a [soft delete](models.md#soft-deletes) marked stay hidden.
        They are a separate switch, `with_deleted()`.
        """
        query = self._copy()
        query.filtered = False
        return query

    def with_deleted(self) -> Self:
        """Include the rows a [soft delete](models.md#soft-deletes) marked.

        Every other filter the model carries stays on.
        """
        query = self._copy()
        query.deleted = INCLUDED
        return query

    def only_deleted(self) -> Self:
        """Read only the rows a soft delete marked.

        ```python
        Note.query.only_deleted().delete(force=True)  # empty the bin
        ```
        """
        query = self._copy()
        query.deleted = ONLY
        return query

    def from_statement(self, statement: ReturnsRowsRole | SupportsClause) -> Self:
        """Take the rows of a statement of your own, mapped onto the model.

        ```python
        User.query.from_statement(sa.text("SELECT * FROM users WHERE ...")).all()
        ```

        Nothing can be added afterwards, and ``__query_filter__`` is not applied:
        what the statement selects is what comes back.
        """
        query = self._copy()
        query._statement = self._select.from_statement(  # noqa: SLF001
            cast("ReturnsRowsRole", statement)
        )
        return query

    def from_sql(self, template: str, /, **context: Any) -> Self:  # noqa: ANN401
        """Take the rows of a SQL template, mapped onto the model.

        ```python
        User.query.from_sql("users/active.sql", team="red").all()
        ```

        Read from the database this query runs on, and rendered for its dialect. As
        with `from_statement`, nothing can be added afterwards and
        ``__query_filter__`` is not applied.
        """
        return self.from_statement(self.db.sql.from_file(template, **context).statement)

    def where(self, *criteria: _ColumnExpressionArgument[bool]) -> Self:
        """Narrow the rows, as `Select.where` does."""
        self._reject_statement("where")
        return self.with_select(self._select.where(*criteria))

    def filter_by(self, **values: Any) -> Self:  # noqa: ANN401
        """Narrow the rows by equality, as `Select.filter_by` does."""
        self._reject_statement("filter_by")
        return self.with_select(self._select.filter_by(**values))

    def join(
        self,
        target: Any,  # noqa: ANN401
        onclause: Any = None,  # noqa: ANN401
        *,
        isouter: bool = False,
        full: bool = False,
    ) -> Self:
        """Join another table in."""
        self._reject_statement("join")
        return self.with_select(
            self._select.join(target, onclause, isouter=isouter, full=full)
        )

    def outerjoin(self, target: Any, onclause: Any = None) -> Self:  # noqa: ANN401
        """Join another table in, keeping the rows without a match."""
        return self.join(target, onclause, isouter=True)

    def select_from(self, *froms: Any) -> Self:  # noqa: ANN401
        """Name what the query selects from, when the joins do not say it."""
        self._reject_statement("select_from")
        return self.with_select(self._select.select_from(*froms))

    def group_by(self, *criteria: Any) -> Self:  # noqa: ANN401
        """Group the rows."""
        self._reject_statement("group_by")
        return self.with_select(self._select.group_by(*criteria))

    def having(self, *criteria: _ColumnExpressionArgument[bool]) -> Self:
        """Narrow the groups."""
        self._reject_statement("having")
        return self.with_select(self._select.having(*criteria))

    def distinct(self) -> Self:
        """Drop the duplicate rows."""
        self._reject_statement("distinct")
        return self.with_select(self._select.distinct())

    def execution_options(self, **options: Any) -> Self:  # noqa: ANN401
        """Set execution options, `yield_per` and `populate_existing` among them."""
        return self.with_select(self._select.execution_options(**options))

    def order_by(
        self,
        *criteria: Any,  # noqa: ANN401
        ignore_case: bool | Sequence[str] = False,
        nulls: Literal["first", "last"] | None = None,
    ) -> Self:
        """Order the rows, by columns or by the sort strings a request carries.

        A string is `name`, `name.desc`, or `name.desc.nulls_last`, and several of
        them order by each in turn. Names are looked up in what the model offers, so
        a field nobody meant to sort by is refused rather than turned into SQL:

        ```python
        User.query.order_by(request.sort).page(limit=20)
        User.query.order_by("team", "created_at.desc").cursor_page(limit=20)
        User.query.order_by(User.name.desc()).all()
        ```

        A `None` is skipped and a list is taken apart, so a request that names no
        sort, or several, passes straight through.

        ``ignore_case`` compares text without regard to case: `True` for every
        field of this call, or the names of the ones it applies to, for a sort
        that arrived as a list:

        ```python
        User.query.order_by("name", ignore_case=True)
        User.query.order_by(request.sort, ignore_case=["name"])
        ```

        Which SQL that becomes is the dialect's to decide, and
        `CASE_INSENSITIVE_COLLATIONS` names the collation. A cursor cannot page
        it: use it with `page`.

        A model sorts by its own mapped columns. `orderable` says how to offer
        others, including fields that are not columns at all.

        ``nulls`` says where the rows with no value go, `first` or `last`. It
        fills in only what neither the sort string nor the model said, which the
        database would otherwise answer for itself, differently by dialect and
        by direction.

        Raises:
            UnknownOrderFieldError: if a string names a field the model does not
                offer.
            InvalidNullsError: if ``nulls`` is neither `first` nor `last`.

        """
        self._reject_statement("order_by")
        return self.with_select(
            ordered(
                self._select,
                self._orderable(),
                criteria,
                ignore_case=ignore_case,
                nulls=nulls,
                model=self.model,
            )
        )

    def _directed(self, column: Any, *, descending: bool) -> Any:  # noqa: ANN401
        """Return the criterion that orders by a column one way or the other.

        Takes what `order_by` takes: a column, or the name of a field the model
        offers.
        """
        if isinstance(column, str):
            name, _, _ = _parse_sort_field(column)
            return f"{name}.desc" if descending else f"{name}.asc"
        return sa.desc(column) if descending else sa.asc(column)

    def _orderable(self) -> Mapping[str, Any]:
        """Return what this model orders by, which a subclass may narrow."""
        return orderable(self.model)

    def limit(self, limit: int) -> Self:
        """Take at most this many rows."""
        self._reject_statement("limit")
        return self.with_select(self._select.limit(limit))

    def offset(self, offset: int) -> Self:
        """Skip this many rows."""
        self._reject_statement("offset")
        return self.with_select(self._select.offset(offset))

    def options(self, *options: Any) -> Self:  # noqa: ANN401
        """Apply loader options, as `Select.options` does."""
        return self.with_select(self._select.options(*options))

    def joinedload(self, *keys: Any) -> Self:  # noqa: ANN401
        """Load a relationship, and the ones below it, with a JOIN."""
        return self.options(_chain(joinedload, keys))

    def selectinload(self, *keys: Any) -> Self:  # noqa: ANN401
        """Load a relationship, and the ones below it, with a second SELECT."""
        return self.options(_chain(selectinload, keys))

    def subqueryload(self, *keys: Any) -> Self:  # noqa: ANN401
        """Load a relationship, and the ones below it, with a subquery."""
        return self.options(_chain(subqueryload, keys))

    def contains_eager(self, *keys: Any) -> Self:  # noqa: ANN401
        """Read a relationship from a join this query already makes."""
        return self.options(_chain(contains_eager, keys))

    def with_for_update(
        self,
        *,
        nowait: bool = False,
        read: bool = False,
        of: Any = None,  # noqa: ANN401
        skip_locked: bool = False,
        key_share: bool = False,
    ) -> Self:
        """Lock the matched rows.

        Args:
            nowait: Fail rather than wait for a row someone else holds.
            read: Take a shared lock instead of an exclusive one.
            of: Lock the rows of these tables only.
            skip_locked: Pass over the rows someone else holds.
            key_share: Take the weakest lock that still blocks key changes.

        """
        parameters: ForUpdateParameter = {
            "nowait": nowait,
            "read": read,
            "of": of,
            "skip_locked": skip_locked,
            "key_share": key_share,
        }
        self._reject_statement("with_for_update")
        return self.with_select(self._select.with_for_update(**parameters))

    # --- Statements ---

    def _lookup_options(self) -> Sequence[Any]:
        """Return the loader options a lookup by key carries over."""
        self._reject_statement("get")
        select = self._select
        carried = {
            "where": select.whereclause is not None,
            "limit": select._limit_clause is not None,  # noqa: SLF001
            "offset": select._offset_clause is not None,  # noqa: SLF001
            "order_by": bool(select._order_by_clauses),  # noqa: SLF001
            "join": bool(select._setup_joins),  # noqa: SLF001
        }
        present = tuple(name for name, carries in carried.items() if carries)
        if present:
            raise KeyLookupError(present)
        return select._with_options  # noqa: SLF001

    def _lock(self) -> Any:  # noqa: ANN401
        """Return the lock this query asks for, if it asks for one."""
        return self._select._for_update_arg  # noqa: SLF001

    def _lookup_statement(self, ident: Any) -> sa.Select[Any] | None:  # noqa: ANN401
        """Return the select a filtered model needs, or None to look up by key.

        A model that hides rows, through ``__query_filter__`` or a soft delete,
        hides them from a lookup too, and the session cannot answer that from the
        identity map: it knows the row, not whether the filter still admits it.
        """
        unfiltered = (
            getattr(self.model, "__query_filter__", None) is None or not self.filtered
        )
        column = soft_delete_column(self.model)
        if unfiltered and (column is None or self.deleted == INCLUDED):
            return None
        mapper = sa.inspect(self.model, raiseerr=True)
        if isinstance(ident, Mapping):
            criteria = [mapper.columns[name] == value for name, value in ident.items()]
        else:
            values = ident if isinstance(ident, tuple) else (ident,)
            criteria = [
                column == value
                for column, value in zip(mapper.primary_key, values, strict=True)
            ]
        return self._filtered().where(*criteria)

    def _executable(self) -> Any:  # noqa: ANN401
        return self._filtered() if self._statement is None else self._statement

    def _filtered(self) -> sa.Select[Any]:
        """Return the select, with the two filters a model can put on a read.

        ``__query_filter__``, which hides rows for good, and the one hiding rows a
        soft delete marked. They lift separately, with `unfiltered()` and
        `with_deleted()`.
        """
        select = self._select
        criterion = getattr(self.model, "__query_filter__", None)
        if criterion is not None and self.filtered:
            select = select.where(criterion())
        column = soft_delete_column(self.model)
        if column is None or self.deleted == INCLUDED:
            return select
        marked = getattr(self.model, column)
        return select.where(
            marked.is_not(None) if self.deleted == ONLY else marked.is_(None)
        )

    def _reject_statement(self, method: str, advice: str | None = None) -> None:
        if self._statement is not None:
            raise RawStatementError(method, advice)

    def _count_statement(self) -> sa.Select[tuple[int]]:
        self._reject_statement("count")
        return sa.select(sa.func.count()).select_from(
            self._filtered().order_by(None).subquery()
        )

    def _exists_statement(self) -> sa.Select[tuple[bool]]:
        self._reject_statement("exists")
        return sa.select(sa.exists(self._filtered().order_by(None).limit(1).subquery()))

    def _page_statement(self, *, limit: int, offset: int) -> sa.Select[Any]:
        self._reject_statement("page")
        self._require_ordering()
        return self._tiebroken(self._filtered()).limit(limit).offset(offset)

    def _tiebroken(self, select: sa.Select[Any]) -> sa.Select[Any]:
        """Append the key, so rows with equal sort values keep their places.

        Without it the database is free to put a tied row on either page, so one row
        comes twice and another never comes.
        """
        clauses = select._order_by_clauses  # noqa: SLF001
        ordered = {_column_identity(clause) for clause in clauses}
        descending = _direction(clauses[-1])[1] if clauses else False
        missing = [
            column
            for column in map(_as_column, self._cursor_key())
            if _column_identity(column) not in ordered
        ]
        if not missing:
            return select
        return select.order_by(
            *(sa.desc(column) if descending else sa.asc(column) for column in missing)
        )

    def _cursor_statement(self, *, limit: int, cursor: str | None) -> sa.Select[Any]:
        """One row more than asked for, so the caller knows whether more follow.

        Backwards is the same walk with every direction flipped: the rows nearest the
        cursor come first, the limit cuts the far end, and `_cursor_page` turns them
        around again.
        """
        self._reject_statement("cursor_page")
        self._require_ordering()
        ordering, ordering_key = self._keyset()
        backwards = cursor is not None and _is_backwards(cursor)
        if backwards:
            ordering = [
                item._replace(descending=not item.descending) for item in ordering
            ]
        select = (
            self._filtered()
            .order_by(None)
            .order_by(
                *(
                    item.column.desc() if item.descending else item.column.asc()
                    for item in ordering
                )
            )
        )
        if cursor is not None:
            select = select.where(
                _seek(ordering, _decode(cursor, ordering, ordering_key))
            )
        return select.limit(limit + 1)

    def _require_ordering(self) -> None:
        """Refuse a page the database is free to order as it likes."""
        if not self.is_ordered:
            raise UnorderedPageError

    def _keyset(self) -> tuple[list[_Ordering], str]:
        """Return the cursor's ordering and its fingerprint, worked out once."""
        if self._keyset_cache is None:
            ordering = self._keyset_ordering()
            self._keyset_cache = (ordering, _ordering_key(ordering))
        return self._keyset_cache

    def _keyset_ordering(self) -> list[_Ordering]:
        """Return the ordering a cursor walks: what was asked, then the key.

        A cursor compares rows by the columns they are ordered on, so the ordering
        has to end in something unique, or rows sharing the last value fall on both
        sides of a page boundary. The key is the primary key unless the model names
        another with ``__cursor_key__``; columns already in the ordering stay where
        they are.
        """
        mapper = sa.inspect(self.model, raiseerr=True)
        ordering: list[_Ordering] = []
        seen: set[str] = set()
        for clause in self._select._order_by_clauses:  # noqa: SLF001
            column, descending = _direction(clause)
            try:
                attribute = mapper.get_property_by_column(column).key
            except (AttributeError, sa_exc.InvalidRequestError):
                raise UncomparableOrderingError(clause) from None
            if attribute not in seen:
                seen.add(attribute)
                ordering.append(_Ordering(column, descending, attribute))
        for entry in self._cursor_key():
            column = _as_column(entry)
            attribute = mapper.get_property_by_column(column).key
            if attribute not in seen:
                seen.add(attribute)
                descending = ordering[-1].descending if ordering else False
                ordering.append(_Ordering(column, descending, attribute))
        return ordering

    def _update_statement(self, values: Mapping[str, Any]) -> sa.Update:
        return (
            sa.update(self.model).where(*self._bulk_criteria("update")).values(**values)
        )

    def _delete_statement(self, *, force: bool = False) -> sa.Delete | sa.Update:
        """Return the statement that deletes, or the one that marks.

        A model that soft-deletes is marked rather than removed, so that the
        bulk path and `Model.delete()` agree. ``force`` removes the rows.
        """
        criteria = self._bulk_criteria("delete")
        column = soft_delete_column(self.model)
        if column is None or force:
            return sa.delete(self.model).where(*criteria)
        return sa.update(self.model).where(*criteria).values({column: sa.func.now()})

    def _bulk_criteria(self, method: str) -> list[Any]:
        """Return what a bulk statement carries over: the narrowing, nothing else."""
        self._reject_statement(method)
        select = self._filtered()
        carried = {
            "limit": select._limit_clause is not None,  # noqa: SLF001
            "offset": select._offset_clause is not None,  # noqa: SLF001
            "order_by": bool(select._order_by_clauses),  # noqa: SLF001
            "join": bool(select._setup_joins),  # noqa: SLF001
        }
        dropped = tuple(name for name, present in carried.items() if present)
        if dropped:
            raise BulkQueryError(method, dropped)
        return [] if select.whereclause is None else [select.whereclause]

    def _columns_select(self, columns: Sequence[Any]) -> sa.Select[Any]:
        """Return the query, reading the given columns instead of whole rows."""
        self._reject_statement("only_columns")
        return self._filtered().with_only_columns(*columns)

    def _cursor_key(self) -> Sequence[Any]:
        """Return the columns that make a cursor unique."""
        key = getattr(self.model, "__cursor_key__", None)
        if key is not None:
            return key()
        return sa.inspect(self.model, raiseerr=True).primary_key

    def _cursor_page(
        self,
        rows: list[ModelT],
        *,
        limit: int,
        cursor: str | None = None,
    ) -> CursorPage[ModelT]:
        """Trim the extra row, and read the cursors off the page.

        Every cursor points at a row of this page, so one only comes back when there
        is a row to point at.
        """
        backwards = cursor is not None and _is_backwards(cursor)
        more = len(rows) > limit
        items = rows[:limit]
        if backwards:
            items.reverse()
        if not items:
            return CursorPage(items=items)
        if backwards:
            # The page ahead is the one this request came from.
            return CursorPage(
                items=items,
                next_cursor=self._cursor_at(items[-1]),
                previous_cursor=self._cursor_at(items[0], backwards=True)
                if more
                else None,
            )
        return CursorPage(
            items=items,
            next_cursor=self._cursor_at(items[-1]) if more else None,
            previous_cursor=self._cursor_at(items[0], backwards=True)
            if cursor is not None
            else None,
        )

    def _cursor_at(self, row: ModelT, *, backwards: bool = False) -> str:
        """Return the cursor that reads on from this row, one way or the other.

        Raises:
            NullCursorValueError: if the row is NULL in a column of the ordering,
                which nothing compares against.
            UncomparableOrderingError: if the ordering names a column the rows do
                not carry, such as one belonging to a joined table.

        """
        ordering, ordering_key = self._keyset()
        values = []
        for item in ordering:
            value = getattr(row, item.attribute)
            if value is None:
                raise NullCursorValueError(item.attribute)
            values.append(value)
        return _encode(values, backwards=backwards, ordering=ordering_key)


def orderable_columns(model: type[Any]) -> Mapping[str, Any]:
    """Return every mapped column of a model, by name.

    What a model orders by when it declares no ``__orderable__``, and what an
    ``__orderable__`` that adds to the columns rather than replacing them starts
    from:

    ```python
    @classmethod
    def __orderable__(cls) -> Mapping[str, Any]:
        return {
            **orderable_columns(cls),
            "team": OrderBy(Team.name, join=cls.team),
        }
    ```

    Calling `orderable` there instead recurses: it reads the very
    ``__orderable__`` that is running.
    """
    mapper = sa.inspect(model, raiseerr=True)
    return {attr.key: getattr(model, attr.key) for attr in mapper.column_attrs}


def orderable(model: type[Any]) -> Mapping[str, Any]:
    """Return the fields a model can be ordered by name.

    Every mapped column, unless the model says otherwise with ``__orderable__``:
    a tuple of the names it allows, or a classmethod returning a mapping of name
    to what sorts by it.
    """
    fields = getattr(model, "__orderable__", None)
    if fields is None:
        return orderable_columns(model)
    if callable(fields):
        return fields()
    return {name: _mapped_column(model, name) for name in fields}


def _mapped_column(model: type[Any], name: str) -> Any:  # noqa: ANN401
    """Return the model's column of that name, or say the declaration is wrong."""
    column = getattr(model, name, None)
    if not isinstance(column, InstrumentedAttribute):
        raise InvalidOrderFieldError(model.__name__, name)
    return column


def ordered(  # noqa: PLR0913 - the shape of an ordering, not a call site
    select: sa.Select[Any],
    fields: Mapping[str, Any],
    criteria: Iterable[Any],
    *,
    ignore_case: bool | Sequence[str] = False,
    nulls: str | None = None,
    model: type[Any] | None = None,
) -> sa.Select[Any]:
    """Return the statement ordered by these criteria, joining what they need.

    Raises:
        UnknownOrderFieldError: if a name is not one of the fields.
        InvalidNullsError: if ``nulls`` is neither "first" nor "last".

    """
    named = list(_flatten(criteria))
    if not named:
        return select
    if nulls not in (None, "first", "last"):
        raise InvalidNullsError(nulls)
    folded = (
        ignore_case
        if isinstance(ignore_case, bool)
        else {_field_named(one, fields) for one in ignore_case}
    )
    clauses = []
    joined: dict[str, Any] = {}
    for criterion in named:
        clause, join = _ordering_for(criterion, fields, ignore_case=folded, model=model)
        clauses.append(_with_nulls(clause, nulls))
        if join is not None:
            target, onclause, outer = join
            _reject_conflicting_join(joined, target, onclause)
            if not _is_joined(select, target):
                select = select.join(target, onclause, isouter=outer)
    return select.order_by(*clauses)


def _ordering_for(
    criterion: Any,  # noqa: ANN401
    fields: Mapping[str, Any],
    *,
    ignore_case: bool | set[str],
    model: type[Any] | None = None,
) -> tuple[Any, Any]:
    """Return the clause a criterion stands for, and the table it needs."""
    if isinstance(criterion, OrderBy):
        return criterion.expression, (criterion.join, criterion.on, criterion.outer)
    if not isinstance(criterion, str):
        return criterion, None
    asked, descending, nulls = _parse_sort_field(criterion)
    name = _field_named(asked, fields)
    field = fields[name]
    if isinstance(field, OrderBy):
        column, join = field.expression, (field.join, field.on, field.outer)
    else:
        column, join = field, _join_for(field, model)
    if ignore_case is True or (ignore_case is not False and name in ignore_case):
        column = _case_insensitive(column)
    return _sort_clause(column, descending=descending, nulls=nulls), join


def _field_named(asked: str, fields: Mapping[str, Any]) -> str:
    """Return the field a request means, whichever case convention it uses.

    An API sends `userName` for a `user_name` the model declares. The spelling
    is a matter of convention on either side, so it is not what tells a field
    from one nobody offers.

    Raises:
        UnknownOrderFieldError: if no field, or more than one, answers to it.

    """
    if asked in fields:
        return asked
    folded = _fold_name(asked)
    matches = [name for name in fields if _fold_name(name) == folded]
    if len(matches) != 1:
        raise UnknownOrderFieldError(asked, list(fields))
    return matches[0]


def _fold_name(name: str) -> str:
    """Return a name with the case and the separators taken out of it."""
    return name.replace("_", "").replace("-", "").lower()


def _chain(loader: Any, keys: Sequence[Any]) -> Any:  # noqa: ANN401
    option = loader(keys[0])
    for key in keys[1:]:
        option = getattr(option, loader.__name__)(key)
    return option


def _direction(clause: Any) -> tuple[sa.ColumnElement[Any], bool]:  # noqa: ANN401
    """Return the column an ORDER BY clause names, and whether it runs backwards.

    `desc()`, `asc()` and the `nulls_*()` variants each wrap the column in one
    more expression, so unwrap until the column itself is left.
    """
    descending = False
    element = clause
    while isinstance(element, NullsPlacement):
        element = element.clause
    while isinstance(element, sa.UnaryExpression):
        if element.modifier is operators.desc_op:
            descending = True
        elif element.modifier is operators.asc_op:
            descending = False
        element = element.element
    return element, descending


def _column_identity(entry: Any) -> tuple[str | None, str | None]:  # noqa: ANN401
    """Return the table and name an ordering clause sorts by.

    `teams.id` and `players.id` are one name and two columns, and taking one for
    the other drops the tiebreaker a page needs.
    """
    column, _ = _direction(entry)
    table = getattr(column, "table", None)
    return getattr(table, "fullname", None), getattr(column, "key", None)


def _parse_sort_field(field: str) -> tuple[str, bool, str | None]:
    """Split `name[.direction[.nulls]]` into what an ORDER BY needs."""
    name, _, rest = field.partition(".")
    direction, _, nulls = rest.partition(".")
    return name, direction.lower() == "desc", nulls.lower() or None


def _sort_clause(column: Any, *, descending: bool, nulls: str | None) -> Any:  # noqa: ANN401
    """Return the ORDER BY clause for one sort field.

    The direction goes underneath a modifier the model set, such as
    ``sa.nulls_last()``, or the SQL comes out as `NULLS LAST DESC`.
    """
    column, wrapped = _split_nulls(column)
    clause = sa.desc(column) if descending else sa.asc(column)
    nulls = nulls or wrapped
    if nulls == "nulls_first":
        return NullsPlacement(clause, last=False)
    if nulls == "nulls_last":
        return NullsPlacement(clause, last=True)
    return clause


def _with_nulls(clause: Any, nulls: str | None) -> Any:  # noqa: ANN401
    """Return the clause with the nulls it was told to put where it asked for none.

    A sort string and a field the model declared each say where their nulls go.
    This fills in only what neither of them said, which the database would
    otherwise answer for itself, differently by dialect and by direction.
    """
    if nulls is None:
        return clause
    _, already = _split_nulls(clause)
    if already is not None:
        return clause
    return NullsPlacement(clause, last=nulls == "last")


def _flatten(criteria: Iterable[Any]) -> Iterator[Any]:
    """Yield the ordering criteria, taking lists apart and dropping the Nones."""
    for criterion in criteria:
        if criterion is None:
            continue
        if isinstance(criterion, OrderBy):
            yield criterion
        elif isinstance(criterion, (list, tuple, set, frozenset)):
            yield from _flatten(criterion)
        else:
            yield criterion


def _join_for(
    field: Any,  # noqa: ANN401
    model: type[Any] | None,
) -> tuple[Any, None, bool] | None:
    """Return what to join for a field that is a plain column of another table.

    A relationship of the model that reaches that table is the join, condition
    and all, which is what a view with no foreign key needs. Failing that, the
    table itself, and `SQLAlchemy` works the condition out from the key.

    An expression may name several tables or none, so it is left alone:
    `OrderBy` says what to join for those, as it does for an alias, a subquery,
    or a table two relationships reach.
    """
    column = _as_column(field)
    if not isinstance(column, sa.Column):
        return None
    table = getattr(column, "table", None)
    if table is None:
        return None
    return (_relationship_to(model, table) or table, None, True)


def _relationship_to(model: type[Any] | None, table: Any) -> Any:  # noqa: ANN401
    """Return the model's one relationship that reaches this table, if there is one."""
    mapper = None if model is None else sa.inspect(model, raiseerr=False)
    if mapper is None:
        return None
    reaching = [
        getattr(model, relationship.key)
        for relationship in mapper.relationships
        if relationship.entity.persist_selectable is table
    ]
    return reaching[0] if len(reaching) == 1 else None


def _reject_conflicting_join(
    joined: dict[str, Any],
    target: Any,  # noqa: ANN401
    onclause: Any,  # noqa: ANN401
) -> None:
    """Refuse a second join of one table on another condition.

    A statement joins a table once, so the second condition would be dropped
    and the field would order by the first one's rows.

    Raises:
        ConflictingJoinError: if the table is already joined on something else.

    """
    identity = _join_identity(target)
    if identity is None:
        return
    if identity not in joined:
        joined[identity] = onclause
        return
    first = joined[identity]
    if first is None or onclause is None:
        return
    if not first.compare(onclause):
        raise ConflictingJoinError(identity)


def _is_joined(select: sa.Select[Any], target: Any) -> bool:  # noqa: ANN401
    """Whether this statement already reaches what a field needs."""
    wanted = _join_identity(target)
    if wanted is None:
        return False
    joined = {_join_identity(join[0]) for join in select._setup_joins}  # noqa: SLF001
    joined |= {_join_identity(entity) for entity in select.columns_clause_froms}
    return wanted in joined


def _join_identity(target: Any) -> str | None:  # noqa: ANN401
    """Return what a join target stands for, as a name that tells two apart.

    Two aliases of one table are two things to join, and the table's name says
    they are one, so the alias's name counts when there is one.
    """
    selectable = _selectable_of(target)
    if selectable is None:
        return None
    name = getattr(selectable, "fullname", None) or getattr(selectable, "name", None)
    return None if name is None else str(name)


def _selectable_of(target: Any) -> Any:  # noqa: ANN401
    """Return the table, alias or subquery a join target stands for."""
    if isinstance(target, sa.Table | sa.Alias | sa.Subquery):
        return target
    inspected = sa.inspect(target, raiseerr=False)
    if inspected is None:
        return None
    relationship = getattr(inspected, "property", None)
    if relationship is not None:
        return getattr(getattr(relationship, "mapper", None), "selectable", None)
    return getattr(inspected, "selectable", None)


def _case_insensitive(column: Any) -> Any:  # noqa: ANN401
    """Return the column compared without regard to case, if it holds text."""
    inner, nulls = _split_nulls(column)
    if not isinstance(getattr(inner, "type", None), sa.String):
        return column
    folded = CaseInsensitive(inner)
    if nulls == "nulls_last":
        return sa.nulls_last(folded)
    if nulls == "nulls_first":
        return sa.nulls_first(folded)
    return folded


def _split_nulls(column: Any) -> tuple[Any, str | None]:  # noqa: ANN401
    """Separate a column from the nulls modifier wrapped around it."""
    if isinstance(column, NullsPlacement):
        return column.clause, "nulls_last" if column.last else "nulls_first"
    if isinstance(column, sa.UnaryExpression):
        if column.modifier is operators.nulls_last_op:
            return column.element, "nulls_last"
        if column.modifier is operators.nulls_first_op:
            return column.element, "nulls_first"
    return column, None


def _as_column(entry: Any) -> sa.ColumnElement[Any]:  # noqa: ANN401
    """Return the column an attribute stands for, and pass a column through."""
    element = getattr(entry, "__clause_element__", None)
    return entry if element is None else element()


def _seek(
    ordering: Sequence[_Ordering],
    values: Sequence[Any],
) -> sa.ColumnElement[bool]:
    """Everything after the row the cursor points at, in this ordering.

    One direction is a row comparison, which an index matches. Mixed directions
    become the equivalent chain of comparisons.
    """
    descending = {item.descending for item in ordering}
    if len(descending) == 1:
        columns = sa.tuple_(*(item.column for item in ordering))
        row = sa.tuple_(*values)
        return columns < row if descending.pop() else columns > row

    terms = []
    for index, item in enumerate(ordering):
        column = item.column
        after = column < values[index] if item.descending else column > values[index]
        equal = [
            earlier.column == values[position]
            for position, earlier in enumerate(ordering[:index])
        ]
        terms.append(sa.and_(*equal, after))
    return sa.or_(*terms)


def _ordering_key(ordering: Sequence[_Ordering]) -> str:
    """Return a short fingerprint of the ordering, for the cursor to carry.

    A cursor only makes sense under the ordering that produced it, and the
    values alone cannot tell a different one apart when the types line up.
    """
    spelled = "|".join(
        f"{item.attribute}.{'desc' if item.descending else 'asc'}" for item in ordering
    )
    return hashlib.blake2s(spelled.encode(), digest_size=4).hexdigest()


def _encode(values: Sequence[Any], *, backwards: bool = False, ordering: str) -> str:
    """Return the row's values, and the way to read from them, as one token.

    The direction rides along so that a caller has one thing to hand back
    whichever page they asked for, and cannot ask for both at once.
    """
    payload = json.dumps(
        {"v": [_as_json(value) for value in values], "b": backwards, "o": ordering},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _payload(cursor: str) -> dict[str, Any]:
    """Return what a cursor holds, or raise if it was not made here."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise InvalidCursorError from None
    if not isinstance(payload, dict) or not isinstance(payload.get("v"), list):
        raise InvalidCursorError
    return payload


def _is_backwards(cursor: str) -> bool:
    """Whether this cursor reads the page in front of the one it came from."""
    return bool(_payload(cursor).get("b"))


def _decode(
    cursor: str,
    ordering: Sequence[_Ordering],
    ordering_key: str,
) -> list[Any]:
    """Return the values a cursor carries, in the types its columns hold.

    Raises:
        InvalidCursorError: if the cursor came from another ordering, or from
            somewhere else entirely.

    """
    payload = _payload(cursor)
    values = payload["v"]
    if payload.get("o") != ordering_key or len(values) != len(ordering):
        raise InvalidCursorError
    try:
        return [
            _from_json(value, item.column.type)
            for value, item in zip(values, ordering, strict=True)
        ]
    except ValueError:
        raise InvalidCursorError from None


def _as_json(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, str | int | float | bool | None):
        return value
    return str(value)


def _from_json(value: Any, type_: sa.types.TypeEngine[Any]) -> Any:  # noqa: ANN401
    if value is None:
        raise InvalidCursorError
    python_type = type_.python_type
    if isinstance(value, python_type):
        return value
    if hasattr(python_type, "fromisoformat"):
        return python_type.fromisoformat(value)
    return python_type(value)


class _Rows(Protocol[RowT_co]):
    """What the helpers below need of a result: one row, or none."""

    def one(self) -> RowT_co: ...

    def one_or_none(self) -> RowT_co | None: ...


def one_row(rows: _Rows[RowT], name: str) -> RowT:
    """Return the single row, saying what had none or too many.

    Raises:
        InstanceNotFoundError: if there is none.
        MultipleInstancesFoundError: if there is more than one.

    """
    try:
        return rows.one()
    except NoResultFound:
        raise InstanceNotFoundError(name) from None
    except MultipleResultsFound:
        raise MultipleInstancesFoundError(name) from None


def one_row_or_none(rows: _Rows[RowT], name: str) -> RowT | None:
    """Return the single row or None, saying what had too many.

    Raises:
        MultipleInstancesFoundError: if there is more than one.

    """
    try:
        return rows.one_or_none()
    except MultipleResultsFound:
        raise MultipleInstancesFoundError(name) from None
