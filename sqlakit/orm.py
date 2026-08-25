from __future__ import annotations

from contextlib import contextmanager
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    Self,
    TypeVar,
    cast,
    overload,
)

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase

from ._base import DEFAULT_ALIAS
from ._db import Database
from ._model import (
    BaseModel,
    BaseSoftDeletes,
    DatabaseDescriptor,
    soft_delete_column,
    tables_for,
)
from ._query import (
    BaseQuery,
    CursorPage,
    Page,
    one_row,
    one_row_or_none,
    orderable,
    ordered,
)
from ._registry import Databases, db
from .exceptions import InstanceNotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from sqlalchemy.engine import CursorResult, Result, ScalarResult
    from sqlalchemy.sql import Executable
    from sqlalchemy.sql._typing import (
        _ColumnExpressionArgument,
        _TypedColumnClauseArgument,
    )
    from sqlalchemy.sql.selectable import ForUpdateParameter

__all__ = ["Model", "ModelMixin", "Query", "SoftDeletes"]

ModelT = TypeVar("ModelT")
RowT = TypeVar("RowT")
RowT_co = TypeVar("RowT_co", covariant=True)
QueryT = TypeVar("QueryT", bound="Query[Any]")
C0 = TypeVar("C0")
C1 = TypeVar("C1")
C2 = TypeVar("C2")


class Query(BaseQuery[ModelT]):
    """A query on the session of the block it runs in.

    Reached as `Model.query`. Every builder method returns a new query, so one
    can be kept and branched from:

    ```python
    active = User.query.where(User.is_active)
    active.count()
    active.order_by(User.id).all()
    ```
    """

    @classmethod
    def as_descriptor(cls) -> Self:
        """Return this query as the ``query`` attribute of a model.

        ```python
        class UserQuery(Query["User"]):
            def active(self) -> Self:
                return self.where(User.is_active.is_(True))


        class User(Model):
            query = UserQuery.as_descriptor()
        ```

        A type checker reads `User.query` as `UserQuery`, so the methods you added
        are visible on it.
        """
        return cast("Self", QueryDescriptor(cls))

    def __init__(self, model: type[ModelT], db: Database) -> None:
        super().__init__(model, db)

    def _rows(self, statement: Executable) -> ScalarResult[ModelT]:
        """Rows as entities, one per identity.

        A `joinedload` against a collection repeats the parent row once per
        child, and SQLAlchemy asks to be told what to do about it.
        """
        return self.db.session.scalars(statement).unique()

    def get(self, ident: Any) -> ModelT | None:  # noqa: ANN401
        """Look the row up by primary key, or return None.

        Goes through the session's identity map, so a row already loaded costs no
        query. This query's loader options and lock carry over, nothing else does,
        and a query narrowed by `where` is refused rather than ignored. Loader
        options read the row again even when the session holds it.
        """
        options = self._lookup_options()
        statement = self._lookup_statement(ident)
        if statement is not None:
            statement = statement.options(*options)
            if options:
                # Loader options say nothing to a row the session already
                # holds, unless it is told to read that row again.
                statement = statement.execution_options(populate_existing=True)
            return self._rows(statement).one_or_none()
        return self.db.session.get(
            self.model,
            ident,
            options=options,
            with_for_update=self._lock(),
            populate_existing=bool(options),
        )

    def get_one(self, ident: Any) -> ModelT:  # noqa: ANN401
        """Look the row up by primary key.

        Raises:
            InstanceNotFoundError: if there is no such row.

        """
        instance = self.get(ident)
        if instance is None:
            raise InstanceNotFoundError(self.model.__name__)
        return instance

    def all(self) -> Sequence[ModelT]:
        """Return every matching row."""
        return self._rows(self._executable()).all()

    def first(self) -> ModelT | None:
        """Return the first matching row, or None."""
        return self._rows(self.limit(1)._executable()).first()  # noqa: SLF001

    def latest(self, column: Any) -> ModelT | None:  # noqa: ANN401
        """Return the row with the greatest value in this column, or None.

        Takes a column, or the name of a field the model offers.
        """
        return self.order_by(self._directed(column, descending=True)).first()

    def earliest(self, column: Any) -> ModelT | None:  # noqa: ANN401
        """Return the row with the least value in this column, or None.

        Takes a column, or the name of a field the model offers.
        """
        return self.order_by(self._directed(column, descending=False)).first()

    def one(self) -> ModelT:
        """Return the single matching row.

        Raises:
            InstanceNotFoundError: if there is none.
            MultipleInstancesFoundError: if there is more than one.

        """
        return one_row(self._rows(self._executable()), self.model.__name__)

    def one_or_none(self) -> ModelT | None:
        """Return the single matching row, or None.

        Raises:
            MultipleInstancesFoundError: if there is more than one.

        """
        return one_row_or_none(self._rows(self._executable()), self.model.__name__)

    def count(self) -> int:
        """Count the matching rows."""
        return self.db.session.scalar(self._count_statement()) or 0

    def exists(self) -> bool:
        """Check whether any row matches."""
        return bool(self.db.session.scalar(self._exists_statement()))

    def page(self, *, limit: int, offset: int = 0, total: bool = True) -> Page[ModelT]:
        """Read one page of rows, with the total.

        The model's key is appended to the ordering, so a row that ties with another
        keeps its place between requests.

        The total costs a second query over the whole match, and the database walks
        the rows the offset skips; past the first few pages [`cursor_page`][sqlakit.orm.Query.cursor_page] does
        neither. With ``total=False`` there is no counting query: the page reads one
        row more than it shows, which answers `Page.has_next` and leaves
        `Page.total` None.

        Raises:
            UnorderedPageError: if the query names no order.

        """
        if not total:
            rows = list(
                self._rows(self._page_statement(limit=limit + 1, offset=offset)).all()
            )
            return Page(
                items=rows[:limit],
                total=None,
                limit=limit,
                offset=offset,
                has_next=len(rows) > limit,
            )
        statement = self._page_statement(limit=limit, offset=offset)
        counted = self.count()
        if counted <= offset:
            return Page(items=[], total=counted, limit=limit, offset=offset)
        items = self._rows(statement).all()
        return Page(items=items, total=counted, limit=limit, offset=offset)

    def cursor_page(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> CursorPage[ModelT]:
        """Read one page of rows, and the cursors that read the ones either side.

        The page follows the order the query carries, with the model's key appended
        so that rows sharing a value cannot fall on both sides of a boundary. Rows
        inserted in between do not shift it, and a page deep in the table costs the
        same as the first.

        Either cursor of a page goes back in as ``cursor``, and the page it names
        comes out: the direction rides along in the cursor.

        ```python
        page = User.query.order_by("created_at.desc").cursor_page(limit=20)
        older = User.query.order_by("created_at.desc").cursor_page(
            limit=20, cursor=page.next_cursor
        )
        ```

        Raises:
            InvalidCursorError: if the cursor was not made for this ordering.

        """
        statement = self._cursor_statement(limit=limit, cursor=cursor)
        rows = list(self._rows(statement).all())
        return self._cursor_page(rows, limit=limit, cursor=cursor)

    def chunks(self, size: int) -> Iterator[Sequence[ModelT]]:
        """Read every matching row, ``size`` of them at a time.

        One statement, whose rows are fetched in batches rather than all at once,
        for the job that walks a table too large to hold:

        ```python
        with db.transaction():
            for contacts in Contact.query.where(Contact.is_stale).chunks(1000):
                deliver(contacts)
        ```

        The rows come off a cursor the database holds open, so the walk is one
        transaction, and committing part-way through ends it. To commit each batch,
        page through the table with `cursor_page` instead.

        A batch is whole rows, which a `joinedload` of a collection is not: load
        collections with `selectinload` here.
        """
        result = self.db.session.scalars(
            self._executable(), execution_options={"yield_per": size}
        )
        yield from result.partitions(size)

    @overload
    def only_columns(
        self, column: _TypedColumnClauseArgument[C0], /
    ) -> ColumnQuery[C0]: ...

    @overload
    def only_columns(
        self,
        column: _TypedColumnClauseArgument[C0],
        other: _TypedColumnClauseArgument[C1],
        /,
    ) -> ColumnQuery[tuple[C0, C1]]: ...

    @overload
    def only_columns(
        self,
        column: _TypedColumnClauseArgument[C0],
        other: _TypedColumnClauseArgument[C1],
        third: _TypedColumnClauseArgument[C2],
        /,
    ) -> ColumnQuery[tuple[C0, C1, C2]]: ...

    @overload
    def only_columns(
        self, *columns: _TypedColumnClauseArgument[Any]
    ) -> ColumnQuery[Any]: ...

    def only_columns(
        self, *columns: _TypedColumnClauseArgument[Any]
    ) -> ColumnQuery[Any]:
        """Read these columns instead of whole rows.

        ```python
        names = User.query.where(User.is_active).only_columns(User.name).all()
        ```
        """
        return ColumnQuery(
            self.model,
            self._columns_select(columns),
            self.db,
            scalar=len(columns) == 1,
        )

    def create(self, **values: Any) -> ModelT:  # noqa: ANN401
        """Write a new row, and return it as an instance.

        ```python
        user = User.query.create(name="ada", team="red")
        ```

        The row goes through the session, so defaults, relationships and the identity
        map behave as they do for a model that saves itself. What it adds is a write
        that needs no model layer: `Query(User, db).create(...)` works on any mapped
        class.
        """
        instance = self.model(**values)
        self.db.session.add(instance)
        self._persist()
        return instance

    def create_many(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Write these rows in one statement, and return how many.

        ```python
        User.query.create_many([{"name": "ada"}, {"name": "grace"}])
        ```

        Nothing is instantiated and no ORM event fires, which is the point for a job
        that loads a file.
        """
        if not rows:
            return 0
        self.db.session.execute(sa.insert(self.model), list(rows))
        self._persist()
        return len(rows)

    def update(self, values: Mapping[str, Any]) -> int:
        """Write these values to every matching row, and return how many.

        One statement, so the session's objects are updated from the database
        rather than in memory. Only the narrowing carries over.

        Raises:
            BulkQueryError: if the query carries anything a statement drops.

        """
        result = self.db.session.execute(self._update_statement(values))
        self._persist()
        return cast("CursorResult[Any]", result).rowcount

    def delete(self, *, force: bool = False) -> int:
        """Delete every matching row, and return how many.

        A model that [soft-deletes](models.md#soft-deletes) is marked rather
        than removed. Pass ``force`` to remove the rows anyway.

        Raises:
            BulkQueryError: if the query carries anything a statement drops.

        """
        result = self.db.session.execute(self._delete_statement(force=force))
        self._persist()
        return cast("CursorResult[Any]", result).rowcount

    def _persist(self) -> None:
        """Commit what a write left behind, unless a block owns the commit.

        Inside a transaction the statement is part of it and the block decides.
        Outside one there is nobody to commit, and a write that reported rows
        would be rolled back when the connection is returned.
        """
        if not self.db.in_transaction():
            self.db.session.commit()


class ColumnQuery(Generic[RowT]):
    """Rows of the columns asked for, not of the model.

    Built by `Query.only_columns`. One column comes back as its own values,
    several come back as tuples.
    """

    def __init__(
        self,
        model: type[Any],
        select: sa.Select[Any],
        db: Database,
        *,
        scalar: bool,
    ) -> None:
        self.model = model
        self._select = select
        self.db = db
        self.scalar = scalar

    def where(self, *criteria: _ColumnExpressionArgument[bool]) -> Self:
        """Narrow the rows."""
        return self.with_select(self._select.where(*criteria))

    def order_by(
        self,
        *criteria: Any,  # noqa: ANN401
        ci_fields: Sequence[str] = (),
    ) -> Self:
        """Order the rows, by columns or by the names the model offers."""
        return self.with_select(
            ordered(self._select, orderable(self.model), criteria, ci_fields)
        )

    def distinct(self) -> Self:
        """Drop the duplicate rows."""
        return self.with_select(self._select.distinct())

    def limit(self, limit: int) -> Self:
        """Take at most this many rows."""
        return self.with_select(self._select.limit(limit))

    def offset(self, offset: int) -> Self:
        """Skip this many rows."""
        return self.with_select(self._select.offset(offset))

    def with_select(self, select: sa.Select[Any]) -> Self:
        return type(self)(self.model, select, self.db, scalar=self.scalar)

    def _rows(self) -> ScalarResult[Any] | Result[Any]:
        if self.scalar:
            return self.db.session.scalars(self._select)
        return self.db.session.execute(self._select)

    def all(self) -> Sequence[RowT]:
        """Return every matching row."""
        return cast("Sequence[RowT]", self._rows().all())

    def first(self) -> RowT | None:
        """Return the first matching row, or None."""
        return cast("RowT | None", self._rows().first())

    def one(self) -> RowT:
        """Return the single matching row.

        Raises:
            InstanceNotFoundError: if there is none.
            MultipleInstancesFoundError: if there is more than one.

        """
        return cast("RowT", one_row(self._rows(), self.model.__name__))

    def one_or_none(self) -> RowT | None:
        """Return the single matching row, or None.

        Raises:
            MultipleInstancesFoundError: if there is more than one.

        """
        return cast("RowT | None", one_row_or_none(self._rows(), self.model.__name__))


class QueryDescriptor(Generic[QueryT]):
    """Hand out a query bound to the model's database, as `Model.query`.

    Assign one to give a model, or a whole base, a query of your own:

    ```python
    class Base(Model):
        __abstract__ = True

        query = QueryDescriptor(AppQuery)
    ```
    """

    def __init__(self, query_class: type[QueryT]) -> None:
        self.query_class = query_class

    def __get__(self, instance: object | None, owner: type[Any]) -> QueryT:
        return self.query_class(owner, owner.db)


class ModelMixin(BaseModel[Database]):
    """Adds saving, lookup and the current database to a declarative base.

    Mix it into your own base when it carries settings of its own, such as a
    ``type_annotation_map``, a naming convention or ``MappedAsDataclass``:

    ```python
    class Model(ModelMixin, MappedAsDataclass, DeclarativeBase):
        type_annotation_map = {str: sa.Text}
    ```

    `Model` is the same thing with a plain declarative base mixed in. The
    database is the one ``__db__`` names, the ``"default"`` alias of the
    importable registry, or one set with [`set_db`][sqlakit.orm.ModelMixin.set_db].
    """

    __db__: ClassVar[str | Database] = DEFAULT_ALIAS
    __dbs__: ClassVar[Databases] = db

    query: ClassVar[QueryDescriptor[Query[Any]]] = QueryDescriptor(Query)

    db: ClassVar[DatabaseDescriptor[Database]] = DatabaseDescriptor()

    @classmethod
    @contextmanager
    def provisioned_tables(cls, alias: str | None = None) -> Iterator[None]:
        """Create the tables that belong on this model's database, and drop them after.

        What a test session opens once, around everything that needs a schema:

        ```python
        @pytest.fixture(scope="session")
        def tables():
            with Model.provisioned_tables():
                yield
        ```

        With models on more than one database, name the alias, and each database
        gets the tables of the models pointed at it.
        """
        db = cls.db if alias is None else cls.__dbs__[alias]
        with db.provisioned_tables(cls.metadata, tables=tables_for(cls, db)):
            yield

    def save(self) -> Self:
        """Write this instance out.

        Commits when nothing else owns a transaction, and flushes when one
        does, so what was written is there for the queries that follow.

        Raises:
            DetachedInstanceError: if its session has closed.

        """
        self._prepare_save()
        self._persist()
        return self

    def delete(self, *, force: bool = False) -> None:
        """Delete the row for this instance.

        A model that [soft-deletes](models.md#soft-deletes) is marked rather
        than removed. Pass ``force`` to remove the row anyway.
        """
        column = soft_delete_column(type(self))
        if column is None or force:
            self.db.session.delete(self)
        else:
            setattr(self, column, sa.func.now())
        self._persist()

    def merge(self) -> Self:
        """Copy this instance into the current session and return the copy.

        An instance loaded in a block that has since closed needs this before it can
        be saved in another one. The row is read again, so anything changed in
        between is overwritten by what this instance holds.
        """
        return self.db.session.merge(self)

    def refresh(
        self,
        *,
        attribute_names: Iterable[str] | None = None,
        with_for_update: ForUpdateParameter = None,
    ) -> None:
        """Read this instance back from the database.

        Args:
            attribute_names: The attributes to reload, rather than all of them.
                A relationship named here is loaded again too.
            with_for_update: Lock the row while it is read, as
                ``Session.refresh`` takes it: `True` for a plain ``FOR UPDATE``,
                or a mapping such as ``{"read": True}``.

        """
        self.db.session.refresh(
            self,
            attribute_names=attribute_names,
            with_for_update=with_for_update,
        )

    def _persist(self) -> None:
        db = self.db
        if db.in_transaction():
            db.session.flush()
        else:
            db.session.commit()


class Model(ModelMixin, DeclarativeBase):
    """A declarative base whose instances know how to save themselves.

    ```python
    class User(Model):
        __tablename__ = "users"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]


    User(name="ada").save()
    ```
    """

    __abstract__ = True


class SoftDeletes(BaseSoftDeletes):
    """Rows this model marks as deleted instead of removing.

    ```python
    class Note(Model, SoftDeletes):
        __tablename__ = "notes"


    note.delete()  # UPDATE notes SET deleted_at = now()
    note.restore()  # and back
    ```

    Reads skip the marked rows, `get()` included. `Note.query.unfiltered()`
    reads them, and `delete(force=True)` removes them for good.
    """

    def restore(self: Any) -> Any:  # noqa: ANN401 - a mixin, on any model
        """Clear the mark, and save the row."""
        setattr(self, self.__soft_delete__, None)
        return self.save()
