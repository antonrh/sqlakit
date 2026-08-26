from __future__ import annotations

import functools
import itertools
import logging
import time
from contextlib import (
    AbstractContextManager,
    ContextDecorator,
    ExitStack,
    contextmanager,
)
from functools import cached_property
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast, overload

import sqlalchemy as sa
import sqlalchemy.exc
from sqlalchemy.orm import Session, sessionmaker

from ._base import (
    BaseDatabase,
    BaseRetryingTransaction,
    _Lazy,
    _Scope,
    default_backoff,
    fix_sqlite_transactions,
    lazy_session_class,
)
from .exceptions import TransactionRolledBackError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import TracebackType

    from sqlalchemy.engine import Engine

    from ._base import RetryOn, _Scope
    from .orm import Query
    from .sql import SQL

ModelT = TypeVar("ModelT")
_FuncT = TypeVar("_FuncT", bound="Callable[..., Any]")

__all__ = ["Database", "RetryingTransaction", "Transaction"]

logger = logging.getLogger("sqlakit")


class Database(BaseDatabase[sa.Connection, Session]):
    """A SQLAlchemy engine, with its connection and session kept in the context.

    Connections opened by [`connect`][sqlakit.Database.connect] and
    [`transaction`][sqlakit.Database.transaction] are reachable below the block as
    [`connection`][sqlakit.Database.connection], and
    [`session`][sqlakit.Database.session] opens a session on
    the same connection. The engine is built on first use.

    ``engine_args`` and ``session_args`` are merged over ``DEFAULT_ENGINE_ARGS``
    and ``DEFAULT_SESSION_ARGS``; what you pass wins.
    """

    _engine: Engine | None = None
    _sessionmaker: sessionmaker[Session] | None = None

    @cached_property
    def sql(self) -> SQL:
        """The SQL templates of this database.

        ```python
        db = Database(DB_URL, templates="app/sql")

        db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
        ```

        Templates need `sqlakit[sql]`, and nothing else here does, so the layer is
        imported when it is first reached.
        """
        # Here rather than at the top, so that `import sqlakit` stays free of
        # the optional layer and of what it imports.
        from .sql import SQL  # noqa: PLC0415

        return SQL(self)

    def query(self, model: type[ModelT]) -> Query[ModelT]:
        """Build a query over a mapped class, on this database.

        ```python
        db.query(User).where(User.is_active).order_by("name").page(limit=20)
        ```

        Any declarative class works, with no model layer under it. A model
        that has one reaches the same builder as `User.query`, on the database
        the model belongs to.
        """
        # Here rather than at the top: `orm` imports this module.
        from .orm import Query  # noqa: PLC0415

        return Query(model, self)

    @property
    def engine(self) -> Engine:
        """The underlying engine, created on first access."""
        if self._engine is None:
            with self._engine_lock:
                # Two threads reaching this at once would each build one, and
                # the pool of the loser would never be disposed.
                if self._engine is None:
                    self._engine = self._create_engine()
        return self._engine

    def _create_engine(self) -> Engine:
        engine = sa.create_engine(self.url, **self.engine_args)
        fix_sqlite_transactions(engine)
        return engine

    @property
    def connection(self) -> sa.Connection:
        """The connection bound to the current context.

        In a [`session_factory`][sqlakit.Database.session_factory] block that
        has not used its session yet, reading this performs the checkout.

        Raises:
            MissingConnectionError: if no connection is bound.

        """
        return self._reused(self._current_scope())

    def _create_session(self, connection: sa.Connection) -> Session:
        if self._sessionmaker is None:
            self._sessionmaker = sessionmaker(**self.session_args)
        return self._sessionmaker(
            bind=connection,
            **self._session_args_for(connection),
        )

    def _lazy_session(self, cell: _Lazy[sa.Connection]) -> Session:
        args: dict[str, Any] = dict(self.session_args)
        session_class = lazy_session_class(args.pop("class_", Session))
        session = session_class(**args)
        session._sqlakit_checkout = cell.get  # noqa: SLF001
        return session

    @staticmethod
    def _reused(scope: _Scope[sa.Connection, Session]) -> sa.Connection:
        """Return the scope's connection, checking it out first if lazy."""
        if scope.connection is None and scope.checkout is not None:
            scope.connection = scope.checkout.get()
        return cast("sa.Connection", scope.connection)

    @contextmanager
    def connect(self) -> Iterator[sa.Connection]:
        """Open a connection and bind it, or reuse the one already bound."""
        reuse = self._scope_to_reuse()
        if reuse is not None:
            with self._bound(self._reused(reuse)) as connection:
                yield connection
            return
        with self.engine.connect() as opened, self._bound(opened) as connection:
            yield connection

    @overload
    def autocommit(self, func: _FuncT) -> _FuncT: ...

    @overload
    def autocommit(
        self,
        func: None = None,
    ) -> AbstractContextManager[sa.Connection]: ...

    def autocommit(
        self,
        func: _FuncT | None = None,
    ) -> _FuncT | AbstractContextManager[sa.Connection]:
        """Run in ``AUTOCOMMIT``, where every statement commits on its own.

        For read-only work, which has nothing to commit, and for statements that
        cannot run inside a transaction: ``VACUUM``, ``CREATE DATABASE``,
        ``CREATE INDEX CONCURRENTLY``. Inside a transaction it joins that one, since
        there is nothing else to commit into.

        A context manager and a decorator, with or without parentheses.

        Args:
            func: The function to decorate, when used as a bare decorator.

        """
        autocommit = self._autocommit()
        if func is not None:
            return autocommit(func)
        return autocommit

    @contextmanager
    def _autocommit(self) -> Iterator[sa.Connection]:
        """Open a connection in ``AUTOCOMMIT`` and bind it, or join the outer one."""
        outer = self._connection_to_join()
        if outer is not None:
            with self._bound(outer, commit=True) as connection:
                yield connection
            return
        with self.engine.connect() as opened:
            opened.execution_options(isolation_level="AUTOCOMMIT")
            with self._set_outer(None), self._bound(opened, commit=True) as connection:
                yield connection

    @overload
    def transaction(self, func: _FuncT) -> _FuncT: ...

    @overload
    def transaction(
        self,
        func: None = None,
        *,
        savepoint: bool = False,
        join_nested: bool = True,
        rollback: bool = False,
        commit_on_error: type[BaseException]
        | tuple[type[BaseException], ...]
        | None = None,
        retry_on: None = None,
        max_retries: int = 3,
        backoff: Callable[[int], float] = default_backoff,
    ) -> Transaction: ...

    @overload
    def transaction(
        self,
        func: None = None,
        *,
        savepoint: bool = False,
        join_nested: bool = True,
        rollback: bool = False,
        commit_on_error: type[BaseException]
        | tuple[type[BaseException], ...]
        | None = None,
        retry_on: RetryOn,
        max_retries: int = 3,
        backoff: Callable[[int], float] = default_backoff,
    ) -> RetryingTransaction: ...

    def transaction(  # noqa: PLR0913  (all keyword-only; this is the main API)
        self,
        func: _FuncT | None = None,
        *,
        savepoint: bool = False,
        join_nested: bool = True,
        rollback: bool = False,
        commit_on_error: type[BaseException]
        | tuple[type[BaseException], ...]
        | None = None,
        retry_on: RetryOn | None = None,
        max_retries: int = 3,
        backoff: Callable[[int], float] = default_backoff,
    ) -> _FuncT | Transaction | RetryingTransaction:
        """Run a transaction on a connection bound to the current context.

        Commits when the block exits, rolls back if it raises. Inside another
        transaction it takes part in that one rather than opening a second
        connection: the outermost block commits.

        A context manager and a decorator, the latter with or without parentheses:

        ```python
        with db.transaction():
            ...


        @db.transaction
        def import_users() -> None: ...
        ```

        Args:
            func: The function to decorate, when used as a bare decorator.
            savepoint: Run as a savepoint when nested, so this block can fail and be
                rolled back on its own, and the blocks below it with it. Off by
                default: a savepoint costs a round trip.
            join_nested: Whether blocks below reuse this connection. Turn it off to
                let them reach the database on their own, seeing nothing of this
                block and surviving its rollback.
            rollback: Roll back on the way out rather than commit. Implies
                ``savepoint``, and is what wraps a test.
            commit_on_error: Exception types whose escape still commits. The
                exception propagates; what was written before it stays.
            retry_on: Exception types, or a predicate over the exception, worth
                another attempt. Decorator only: retrying re-runs the block, so this
                returns a [`RetryingTransaction`][sqlakit.RetryingTransaction] that type
                checkers refuse to
                enter. Only the block that owns the transaction retries.
            max_retries: How many further attempts ``retry_on`` may buy.
            backoff: Seconds to wait before attempt ``n``, counted from zero.

        """

        def new_transaction() -> Transaction:
            return Transaction(
                self,
                savepoint=savepoint,
                join_nested=join_nested,
                rollback=rollback,
                commit_on_error=commit_on_error,
            )

        transaction: Transaction | RetryingTransaction = new_transaction()
        if retry_on is not None:
            transaction = RetryingTransaction(
                new_transaction,
                retry_on=retry_on,
                max_retries=max_retries,
                backoff=backoff,
            )
        if func is not None:
            return transaction(func)
        return transaction

    @contextmanager
    def session_factory(self) -> Iterator[Session]:
        """Open a session for the block, and bind it.

        The session is created here, as ``sessionmaker()`` would, and like one
        of those it takes no connection from the pool until it needs one: the
        checkout happens on the first query or flush, not on ``add()``. A
        block that never uses the session never touches the database. Inside
        another block it runs on the connection already bound.
        """
        reuse = self._scope_to_reuse()
        if reuse is not None:
            with self._bound(self._reused(reuse)):
                yield self.session
            return
        # The lambda defers `self.engine` too: no engine until first use.
        cell: _Lazy[sa.Connection] = _Lazy(lambda: self.engine.connect())  # noqa: PLW0108
        with self._bind(None, checkout=cell) as scope:
            try:
                yield self.session
            finally:
                try:
                    if scope.session is not None:
                        scope.session.close()
                finally:
                    if cell.connection is not None:
                        cell.connection.close()

    @contextmanager
    def _bound(
        self,
        connection: sa.Connection,
        *,
        commit: bool = False,
    ) -> Iterator[sa.Connection]:
        """Bind ``connection`` for the block, ending the session it opened.

        ``commit`` keeps that session's work. Closing a session that isolates
        itself with a savepoint rolls back to it, discarding what the blocks
        below committed.
        """
        with self._bind(connection) as scope:
            done = False
            try:
                yield connection
                done = True
            finally:
                if scope.session is not None:
                    if commit and done:
                        scope.session.commit()
                    scope.session.close()

    @contextmanager
    def provisioned_tables(
        self,
        metadata: sa.MetaData,
        *,
        tables: Sequence[sa.Table] | None = None,
    ) -> Iterator[None]:
        """Create these tables here, and drop them when the block ends.

        What a test session opens once, around everything that needs a schema:

        ```python
        @pytest.fixture(scope="session")
        def tables():
            with db.provisioned_tables(Model.metadata):
                yield
        ```

        Every table of the metadata unless ``tables`` names fewer, which is what a
        second database wants.
        """
        with self.transaction() as connection:
            metadata.create_all(connection, tables=tables)
        try:
            yield
        finally:
            with self.transaction() as connection:
                metadata.drop_all(connection, tables=tables)

    def ping(self) -> bool:
        """Whether the database answers."""
        try:
            with self.engine.connect() as connection:
                connection.execute(sa.text("SELECT 1"))
        except sa.exc.SQLAlchemyError:
            return False
        return True

    def dispose(self, *, close: bool = True) -> None:
        """Dispose of the engine and its connection pool."""
        with self._engine_lock:
            if self._engine is not None:
                self._engine.dispose(close=close)
                self._engine = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.dispose()


class Transaction(ContextDecorator, AbstractContextManager["sa.Connection"]):
    """What [`Database.transaction`][sqlakit.Database.transaction] returns.

    A class rather than a generator, so that it works as a decorator and can be
    entered more than once, which is what decorating a function does.
    """

    def __init__(
        self,
        db: Database,
        *,
        savepoint: bool = False,
        join_nested: bool = True,
        rollback: bool = False,
        commit_on_error: type[BaseException]
        | tuple[type[BaseException], ...]
        | None = None,
    ) -> None:
        self.db = db
        self.savepoint = savepoint
        self.join_nested = join_nested
        self.rollback = rollback
        self.commit_on_error = commit_on_error
        self._stacks: list[ExitStack] = []

    def _recreate_cm(self) -> Transaction:
        """Give every decorated call a transaction of its own.

        `ContextDecorator` reuses one instance for all calls, and two of them
        running at once would unwind each other's blocks.
        """
        return Transaction(
            self.db,
            savepoint=self.savepoint,
            join_nested=self.join_nested,
            rollback=self.rollback,
            commit_on_error=self.commit_on_error,
        )

    def __enter__(self) -> sa.Connection:
        stack = ExitStack()
        try:
            outer, savepoint = self.db._plan(  # noqa: SLF001
                savepoint=self.savepoint,
                rollback=self.rollback,
            )
            if outer is not None:
                connection = outer.connection
                # Without a savepoint the block only takes part in the
                # transaction around it, which commits it.
                transaction = connection.begin_nested() if savepoint else None
                # The block's savepoint isolates it; a session opened inside
                # must not add a second one on the same connection.
                session_savepoint = outer.session_savepoint and not savepoint
            else:
                connection = stack.enter_context(self.db.engine.connect())
                transaction = connection.begin()
                session_savepoint = savepoint
            # Unwound in reverse: session, context, transaction, connection.
            stack.push(self._finish(transaction))
            bound = stack.enter_context(
                self.db._set_outer(  # noqa: SLF001
                    connection,
                    join_nested=self.join_nested,
                    savepoint=savepoint,
                    session_savepoint=session_savepoint,
                )
            )
            scope = stack.enter_context(self.db._bind(connection))  # noqa: SLF001
            if bound is not None:
                bound.scope = scope
            stack.push(self._close_session(scope))
        except BaseException:
            stack.close()
            raise
        self._stacks.append(stack)
        return connection

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stacks.pop().__exit__(exc_type, exc, traceback)

    def _close_session(
        self,
        scope: _Scope[sa.Connection, Session],
    ) -> Callable[..., None]:
        """Commit the block's session, if it opened one, then close it.

        The block is the unit of work, so what its session holds belongs to the
        transaction; closing first would roll it back.
        """

        def close_session(
            _exc_type: object,
            exc: BaseException | None,
            _traceback: object,
        ) -> None:
            if scope.session is None:
                return
            if self._keeps(exc):
                scope.session.commit()
            scope.session.close()

        return close_session

    def _finish(self, transaction: sa.Transaction | None) -> Callable[..., None]:
        """Commit or roll back, unless this block only takes part in another."""

        def finish(
            _exc_type: object,
            exc: BaseException | None,
            _traceback: object,
        ) -> None:
            if transaction is None:
                return
            if not transaction.is_active:
                # Rolled back from inside the block. Say so, unless an
                # exception is already on its way out with the reason.
                if exc is None:
                    raise TransactionRolledBackError
                return
            if self._keeps(exc) and not self.rollback:
                transaction.commit()
            else:
                transaction.rollback()

        return finish

    def _keeps(self, exc: BaseException | None) -> bool:
        """Whether the block's work is kept rather than undone."""
        if exc is None:
            return True
        return self.commit_on_error is not None and isinstance(
            exc, self.commit_on_error
        )


class RetryingTransaction(BaseRetryingTransaction):
    """A transaction that runs its block again.

    What [`Database.transaction`][sqlakit.Database.transaction] returns when it is
    given ``retry_on``.
    """

    def __call__(self, func: _FuncT) -> _FuncT:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            transaction = self.transaction()
            if transaction.db.in_transaction():
                # Only the block that owns the transaction can restart it: a
                # retry inside would keep the snapshot that caused the
                # conflict, and the outer transaction fails anyway.
                logger.debug(
                    "%s runs inside another transaction; retrying is up to "
                    "whoever opened it.",
                    getattr(func, "__qualname__", func),
                )
                with transaction:
                    return func(*args, **kwargs)
            for attempt in itertools.count():
                try:
                    with self.transaction():
                        return func(*args, **kwargs)
                except Exception as exc:
                    if not self._retry(exc, attempt=attempt):
                        raise
                    time.sleep(self.backoff(attempt))
            raise AssertionError  # pragma: no cover - the loop returns or raises

        return cast("_FuncT", wrapper)
