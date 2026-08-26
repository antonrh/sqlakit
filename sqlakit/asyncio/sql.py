from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

import sqlalchemy as sa

from sqlakit._sql import (
    BaseSQLQuery,
    Templates,
    require_pydantic,
    templates_of,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from sqlalchemy.engine import Result, ScalarResult
    from sqlalchemy.ext.asyncio import AsyncConnection
    from sqlalchemy.sql import Executable

    from ._db import Database

__all__ = ["SQL", "SQLQuery", "SQLRows", "Templates"]

RowT = TypeVar("RowT")
OtherT = TypeVar("OtherT")


class SQL:
    """The SQL templates of one database, awaited.

    Reached as `db.sql`, and where the templates are is the database's own
    `templates=`:

    ```python
    db = Database(DB_URL, templates="app/sql")

    await db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
    await db.sql.from_string("SELECT count(*) FROM users").scalars().one()
    ```
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.db!r})"

    def __call__(self, template: str, **context: Any) -> SQLQuery:  # noqa: ANN401
        """Read the rows of a template. Short for `from_file`.

        ```python
        await db.sql("users/active.sql", team="red").all()
        ```
        """
        return self.from_file(template, **context)

    def from_file(self, template: str, **context: Any) -> SQLQuery:  # noqa: ANN401
        """Read the rows of a template kept under the database's ``templates=``.

        ```python
        await db.sql.from_file("users/active.sql", team="red").all()
        ```

        The keyword arguments are the template's context.
        """
        return SQLQuery(self.db, template, context)

    def from_string(self, source: str, **context: Any) -> SQLQuery:  # noqa: ANN401
        """Read the rows of SQL written out here rather than kept in a file.

        ```python
        await db.sql.from_string(
            "SELECT id FROM users WHERE team = {{ team }}", team="red"
        )
        ```

        Values are named in `{{ }}` and passed by keyword, as in a template. A
        `:name` or a `?` binds nothing here, and rendering says so rather than
        reaching the driver. It needs no ``templates=``.
        """
        return SQLQuery(self.db, source, context, inline=True)

    def from_statement(self, statement: Executable) -> SQLQuery:
        """Read the rows of a statement built with SQLAlchemy.

        ```python
        await db.sql.from_statement(sa.text("SELECT ...").bindparams(id=1)).all()
        ```

        Nothing is rendered: the statement is the one that runs, parameters
        and all. What this adds is the reading, `typed` and `chunks` included.
        """
        return SQLQuery(self.db, statement, {})

    @property
    def templates(self) -> Templates:
        """Where this database looks for its templates."""
        return templates_of(self.db)

    def check(self) -> None:
        """Compile every `.sql` template, so a broken one fails where deploys do.

        Call it at startup, next to the rest of the wiring: a template is read
        when something asks for it, and that is a poor time to find a typo.
        """
        self.templates.check()


class SQLRows(BaseSQLQuery[RowT, "Database"]):
    """The rows of a SQL template, on the connection of the block it runs in.

    What the rows are is settled: reading them is all that is left.
    """

    async def all(self) -> Sequence[RowT]:
        """Return every row."""
        rows = await self._rows()
        return cast("Sequence[RowT]", self._shaped(rows.all()))

    async def first(self) -> RowT | None:
        """Return the first row, or None."""
        rows = await self._rows()
        return cast("RowT | None", self._shaped_one(rows.first()))

    async def one(self) -> RowT:
        """Return the single row.

        Raises:
            NoResultFound: if there is none.
            MultipleResultsFound: if there is more than one.

        """
        rows = await self._rows()
        return cast("RowT", self._shaped_one(rows.one()))

    async def one_or_none(self) -> RowT | None:
        """Return the single row, or None.

        Raises:
            MultipleResultsFound: if there is more than one.

        """
        rows = await self._rows()
        return cast("RowT | None", self._shaped_one(rows.one_or_none()))

    async def chunks(self, size: int) -> AsyncIterator[Sequence[RowT]]:
        """Read every row, ``size`` of them at a time.

        One statement, fetched in batches, for a job that walks a table too large to
        hold. The rows come off a cursor the database holds open, so the whole walk
        is one transaction.
        """
        connection = await self._connection()
        streamed = await connection.stream(self._executable(size=size))
        rows = streamed.scalars() if self.scalar else streamed
        async for batch in rows.partitions(size):
            yield cast("Sequence[RowT]", self._shaped(batch))

    async def execute(self) -> int:
        """Run it for what it writes, and return how many rows it touched.

        For a template that inserts, updates or deletes. Inside a transaction the
        write is part of it and the block decides. In a block with no transaction
        the call commits for itself, as ORM writes do.
        """
        connection = await self._connection()
        result = await connection.execute(self.statement)
        if not self.db.in_transaction():
            await connection.commit()
        return result.rowcount

    async def _rows(self) -> Result[Any] | ScalarResult[Any]:
        connection = await self._connection()
        result = await connection.execute(self.statement)
        return result.scalars() if self.scalar else result

    async def _connection(self) -> AsyncConnection:
        """Return this block's connection, with any pending ORM writes on it."""
        if self.db.in_session():
            await self.db.session.flush()
        return await self.db._aconnection()  # noqa: SLF001


class SQLQuery(SQLRows[sa.Row[Any]]):
    """The rows a SQL template returns, as `db.sql(...)` hands them over.

    `typed` and `scalars` say what one row is; both return rows that read the
    same way and carry no further say, so each is asked once.
    """

    def typed(self, type_: type[OtherT], /) -> SQLRows[OtherT]:
        """Read the rows as this type, one row at a time.

        ```python
        await db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
        ```

        The type is what one row becomes, and the terminal decides the container.
        Anything pydantic can validate works: a model, a dataclass, a
        `TypedDict`. The type also says how much of the row it takes: one built
        from columns is given the whole row, and anything else is given the
        first column, so `SELECT count(*)` with `typed(int)` reads as an `int`.

        Raises:
            MissingDependencyError: if pydantic is not installed.

        """
        require_pydantic()
        return cast("SQLRows[OtherT]", self._as(SQLRows, type_=type_))

    def scalars(self) -> SQLRows[Any]:
        """Read the first column of each row instead of whole rows.

        ```python
        await db.sql.from_string("SELECT count(*) FROM users").scalars().one()
        ```

        `typed()` does the same when the type is worth naming; this is for when it is
        not, and needs no pydantic.
        """
        return self._as(SQLRows, scalar=True)
