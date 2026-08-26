from __future__ import annotations

import asyncio
import inspect
import random
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Self,
    TypeVar,
    cast,
    overload,
)

import sqlalchemy as sa
import sqlalchemy.event
from typing_extensions import Unpack

from ._discovery import import_string
from ._recording import (
    Recording,
    Statement,
    caller_stack,
    check,
    require_expectation,
)
from ._routing import Router, as_router
from .exceptions import (
    DEFAULT_ALIAS,
    ConflictingDatabaseUrlError,
    DatabaseAlreadyConfiguredError,
    DatabaseNotConfiguredError,
    MissingConnectionError,
    MissingDatabaseUrlError,
    MissingDefaultDatabaseError,
    MissingSessionError,
    RetryNotSupportedError,
    UnknownDatabaseError,
)

if TYPE_CHECKING:
    import logging
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from ._sql import Templates
    from .types import DatabaseConfig, EngineArgs, SessionArgs, UrlParts

    RouterFunction = Callable[[type[Any]], str | None]
    TemplatesLike = str | Path | Sequence[str | Path] | Templates
    """Where a database's SQL templates are: a path, several, or the object."""

__all__ = [
    "DEFAULT_ALIAS",
    "DEFAULT_ENGINE_ARGS",
    "DEFAULT_SESSION_ARGS",
    "BaseDatabase",
    "BaseRetryingTransaction",
    "RetryOn",
]

DEFAULT_ENGINE_ARGS: EngineArgs = {
    # Catch connections dropped by the server, a proxy or a failover.
    "pool_pre_ping": True,
    # Reopen before the idle timeouts of MySQL, PgBouncer and cloud balancers.
    "pool_recycle": 1800,
}

DEFAULT_SESSION_ARGS: SessionArgs = {
    # Attributes stay readable after a commit. Under asyncio the lazy SELECT
    # that expiry would trigger fails with MissingGreenlet.
    "expire_on_commit": False,
}


RetryOn = (
    type[BaseException]
    | tuple[type[BaseException], ...]
    | Callable[[BaseException], bool]
)
"""Exception types, or a predicate over the exception."""

_random = random.SystemRandom()

ConnectionT = TypeVar("ConnectionT")
SessionT = TypeVar("SessionT")


class _Lazy(Generic[ConnectionT]):
    """A connection checkout that has not happened yet.

    A lazy ``session_factory()`` block binds one of these instead of a
    connection. ``get`` and ``aget`` perform the checkout on first use, once,
    and cache the connection.
    """

    __slots__ = ("_alock", "_lock", "connection", "open")

    def __init__(self, open: Callable[[], Any]) -> None:  # noqa: A002
        self.open = open
        self.connection: ConnectionT | None = None
        self._lock = threading.Lock()
        self._alock: asyncio.Lock | None = None

    def get(self) -> ConnectionT:
        """Materialize the connection, once, and return it."""
        with self._lock:
            if self.connection is None:
                self.connection = self.open()
        return self.connection

    async def aget(self) -> ConnectionT:
        """Materialize the connection, once, awaited."""
        if self.connection is not None:
            return self.connection
        # On the cell, made inside a running coroutine: a cell lives within
        # one block on one loop, while the database outlives loops.
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            if self.connection is None:
                connection = self.open()
                if inspect.isawaitable(connection):
                    connection = await connection
                self.connection = connection
        return self.connection


@dataclass(slots=True)
class _Scope(Generic[ConnectionT, SessionT]):
    """The connection bound to a context, and the session opened on top of it.

    The context variable holds this object, not the session, so a session
    opened later, including in a task that copies the context, still
    belongs to the block that bound the connection.

    A lazy ``session_factory()`` block binds a scope with no connection and a
    ``checkout`` instead. The connection lands here once something uses it.
    """

    connection: ConnectionT | None
    session: SessionT | None = None
    checkout: _Lazy[ConnectionT] | None = None


@dataclass(slots=True)
class _Outer(Generic[ConnectionT]):
    """The connection of the innermost transaction bound to a context.

    Args:
        connection: What blocks below reuse, with ``join_nested``.
        join_nested: Whether blocks below reuse that connection.
        savepoint: Whether nested blocks run as savepoints.
        session_savepoint: Whether this block's session needs a savepoint of its
            own. Two savepoint owners on one connection release each other's out
            of order, so only one may have it.
        scope: The block's own scope, whose session the savepoint is for.

    """

    connection: ConnectionT
    join_nested: bool = True
    savepoint: bool = False
    session_savepoint: bool = False
    scope: Any = None


class BaseDatabase(Generic[ConnectionT, SessionT]):
    """What the sync and async databases share: everything that is not IO.

    Binding to the context lives here. Opening and closing connections and
    sessions is left to the subclass.
    """

    _engine: Any = None  # narrowed by the subclass

    def __init__(
        self,
        url: str | sa.URL | None = None,
        engine_args: EngineArgs | None = None,
        session_args: SessionArgs | None = None,
        templates: TemplatesLike | None = None,
        **parts: Unpack[UrlParts],
    ) -> None:
        """Build a database on ``url``, or on the parts to make one from.

        ```python
        Database("postgresql+psycopg://localhost/app")

        Database(
            drivername="postgresql+psycopg",
            host="localhost",
            database="app",
        )

        Database(DB_URL, templates="app/sql")  # where `sql` reads templates from
        ```

        Raises:
            MissingDatabaseUrlError: if given neither a ``url`` nor the parts to
                build one.
            ConflictingDatabaseUrlError: if given both. Either is an
                ``InvalidDatabaseConfigError``.

        """
        config = cast("DatabaseConfig", dict(parts))
        if url is not None:
            config["url"] = url
        self.url = sa.make_url(url_from_config(config))
        self.templates = templates
        self.engine_args = DEFAULT_ENGINE_ARGS | (engine_args or {})
        self.session_args = DEFAULT_SESSION_ARGS | (session_args or {})
        # Dropped so that reconfiguring does not keep sessions built from the
        # arguments of the previous configuration.
        self._sessionmaker = None
        self._engine_lock = threading.Lock()
        self._scope: ContextVar[_Scope[ConnectionT, SessionT]] = ContextVar(
            f"{type(self).__name__}.scope"
        )
        self._outer: ContextVar[_Outer[ConnectionT] | None] = ContextVar(
            f"{type(self).__name__}.outer"
        )
        self._recordings: ContextVar[tuple[Recording, ...]] = ContextVar(
            f"{type(self).__name__}.recordings", default=()
        )
        self._stacks: ContextVar[bool] = ContextVar(
            f"{type(self).__name__}.stacks", default=False
        )
        self._listening = 0
        self._listening_lock = threading.Lock()
        self._name = DEFAULT_ALIAS

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.url.render_as_string()!r})"

    def _current_scope(self) -> _Scope[ConnectionT, SessionT]:
        """Return the scope bound to the current context.

        Raises:
            MissingConnectionError: if no block is open.

        """
        try:
            return self._scope.get()
        except LookupError:
            raise MissingConnectionError from None

    @property
    def session(self) -> SessionT:
        """The session bound to the current context.

        Opened on first use, on the current
        [`connection`][sqlakit.Database.connection], and closed when that block
        exits.

        Raises:
            MissingSessionError: if no connection is bound.

        """
        try:
            scope = self._scope.get()
        except LookupError:
            raise MissingSessionError from None
        if scope.session is None:
            if scope.connection is None and scope.checkout is not None:
                scope.session = self._lazy_session(scope.checkout)
            else:
                scope.session = self._create_session(
                    cast("ConnectionT", scope.connection)
                )
        return scope.session

    @contextmanager
    def recording(
        self,
        label: str | None = None,
        *,
        logger: logging.Logger | None = None,
        echo: bool = False,
        stacks: bool = False,
        into: Recording | None = None,
    ) -> Iterator[Recording]:
        """Record the statements of this block, and what they add up to.

        ```python
        logger = logging.getLogger(__name__)

        with db.recording("GET /users", logger=logger) as record:
            build_report()

        record.count, record.duplicates, record.slowest
        ```

        ``logger`` writes a summary when the block ends, at a level the numbers
        choose. ``echo`` prints the statements instead, coloured where `rich` is
        installed. ``stacks`` has every statement remember the frames that led to it,
        at the cost of a stack walk each time.

        Blocks nest, each recording what runs inside it, and the listeners come off
        after. `with` is right on either side, awaited or not: it listens, it does
        not run anything.
        """
        recording = Recording(label=label) if into is None else into
        self._listen()
        recordings = self._recordings.set((*self._recordings.get(), recording))
        asked = self._stacks.set(stacks or self._stacks.get())
        try:
            yield recording
        finally:
            self._stacks.reset(asked)
            self._recordings.reset(recordings)
            self._silence()
            if logger is not None:
                recording.log(logger)
            if echo:
                recording.echo()

    @contextmanager
    def assert_queries(
        self,
        count: int | None = None,
        *,
        at_most: int | None = None,
        duplicates: bool = True,
    ) -> Iterator[Recording]:
        """Assert what the block asks of this database.

        ```python
        with db.assert_queries(2):
            User.query.order_by("name").page(limit=10)

        with db.assert_queries(at_most=5):
            render(dashboard)

        with db.assert_queries(duplicates=False):
            render(users)  # the N+1 test, without a number
        ```

        The three checks stand alone or together. What fails prints the statements,
        numbered and timed, with the repeated ones pointing at each other.

        This watches one database. `sqlakit.testing.assert_queries` watches the
        importable registries instead, or whichever database it is given.

        Args:
            count: The statements the block is expected to run.
            at_most: A ceiling, for a number that would be brittle.
            duplicates: Whether a statement may run more than once.

        Raises:
            TypeError: if there is nothing to assert.

        """
        require_expectation(count, at_most, duplicates)
        with self.recording() as recording:
            yield recording
        check(recording, count=count, at_most=at_most, duplicates=duplicates)

    def _listen(self) -> None:
        with self._listening_lock:
            if self._listening == 0:
                engine = getattr(self.engine, "sync_engine", self.engine)
                sa.event.listen(engine, "before_cursor_execute", self._statement_began)
                sa.event.listen(engine, "after_cursor_execute", self._statement_ended)
            self._listening += 1

    def _silence(self) -> None:
        with self._listening_lock:
            self._listening -= 1
            if self._listening == 0:
                engine = getattr(self.engine, "sync_engine", self.engine)
                sa.event.remove(engine, "before_cursor_execute", self._statement_began)
                sa.event.remove(engine, "after_cursor_execute", self._statement_ended)

    def _statement_began(self, connection: Any, *_arguments: Any) -> None:  # noqa: ANN401
        connection.info.setdefault("sqlakit_started", []).append(time.perf_counter())

    def _statement_ended(
        self,
        connection: Any,  # noqa: ANN401
        _cursor: Any,  # noqa: ANN401
        statement: str,
        parameters: Any,  # noqa: ANN401
        _context: Any,  # noqa: ANN401
        _many: bool,  # noqa: FBT001
    ) -> None:
        starts = connection.info.get("sqlakit_started")
        if not starts:
            # Began before the listeners attached; no start time, not recorded.
            return
        started = starts.pop()
        recordings = self._recordings.get()
        if not recordings or statement.split(None, 1)[0].upper() in _CONTROL:
            return
        record = Statement(
            sql=statement,
            parameters=parameters,
            duration=time.perf_counter() - started,
            database=self._name,
            stack=caller_stack() if self._stacks.get() else (),
        )
        for recording in recordings:
            recording.statements.append(record)

    def in_transaction(self) -> bool:
        """Whether a transaction is bound to the current context.

        False under ``connect()`` and ``autocommit()``, which open none.
        """
        return self._outer.get(None) is not None

    def in_session(self) -> bool:
        """Whether a session is open in the current context.

        A block opens one when something first asks for [`session`][sqlakit.Database.session], so
        this is False in a block that has only run statements on the
        connection. Reading it opens nothing.
        """
        scope = self._scope.get(None)
        return scope is not None and scope.session is not None

    @property
    def engine(self) -> Any:  # noqa: ANN401
        """The engine underneath, which the subclass makes."""
        raise NotImplementedError  # pragma: no cover - the subclass has it

    def _create_session(self, connection: ConnectionT) -> SessionT:
        raise NotImplementedError  # pragma: no cover - the subclass has it

    def _lazy_session(self, cell: _Lazy[ConnectionT]) -> SessionT:
        raise NotImplementedError  # pragma: no cover - the subclass has it

    def _session_args_for(self, connection: ConnectionT) -> dict[str, Any]:
        """How a session joins the transaction already open on ``connection``."""
        outer = self._outer.get(None)
        if outer is None or outer.connection is not connection:
            return {}
        if outer.session_savepoint and outer.scope is self._scope.get(None):
            return {"join_transaction_mode": "create_savepoint"}
        # Spelled out: SQLAlchemy's default would open a savepoint of its own
        # whenever the connection is already inside one.
        return {"join_transaction_mode": "rollback_only"}

    def _plan(self, *, savepoint: bool, rollback: bool) -> tuple[_Outer | None, bool]:
        """Decide what a transaction joins, and whether it is a savepoint.

        A block to be rolled back needs a savepoint to roll back to, and a
        block inside an isolated one stays isolated.
        """
        outer = self._outer_to_join()
        return outer, savepoint or rollback or bool(outer and outer.savepoint)

    def _outer_to_join(self) -> _Outer[ConnectionT] | None:
        """Return the outer transaction new blocks join, if there is one."""
        outer = self._outer.get(None)
        return outer if outer is not None and outer.join_nested else None

    def _connection_to_join(self) -> ConnectionT | None:
        outer = self._outer_to_join()
        return outer.connection if outer is not None else None

    def _scope_to_reuse(self) -> _Scope[ConnectionT, SessionT] | None:
        """Return the bound scope a new block reuses, if it may.

        One connection per context: a block that only needs a connection takes
        the one already bound, whether a transaction, ``autocommit()`` or
        another ``connect()`` opened it. ``join_nested=False`` opts out. A
        lazy scope has to be materialized before its connection is reused,
        which the caller does, awaited or not.
        """
        outer = self._outer.get(None)
        if outer is not None and not outer.join_nested:
            return None
        return self._scope.get(None)

    @contextmanager
    def _bind(
        self,
        connection: ConnectionT | None,
        checkout: _Lazy[ConnectionT] | None = None,
    ) -> Iterator[_Scope[ConnectionT, SessionT]]:
        """Bind a scope holding ``connection`` to the current context.

        Every block gets a scope, and so a session, of its own. Ending that
        session is left to the caller, which knows whether it takes an
        ``await``. A lazy block passes ``checkout`` instead of a connection.
        """
        scope = _Scope[ConnectionT, SessionT](connection, checkout=checkout)
        token = self._scope.set(scope)
        try:
            yield scope
        finally:
            self._scope.reset(token)

    @contextmanager
    def _set_outer(
        self,
        connection: ConnectionT | None,
        *,
        join_nested: bool = True,
        savepoint: bool = False,
        session_savepoint: bool = False,
    ) -> Iterator[_Outer[ConnectionT] | None]:
        """Make ``connection`` the outer one for this context. See `_Outer`.

        ``None`` leaves this context without an outer transaction at all, which
        is what ``autocommit()`` needs: blocks under it must not join a
        transaction its own connection is not part of.
        """
        outer = (
            _Outer(
                connection,
                join_nested=join_nested,
                savepoint=savepoint,
                session_savepoint=session_savepoint,
            )
            if connection is not None
            else None
        )
        token = self._outer.set(outer)
        try:
            yield outer
        finally:
            self._outer.reset(token)


DatabaseT = TypeVar("DatabaseT", bound="BaseDatabase[Any, Any]")


class _Using:
    """A database standing in for the default one, while a block of it is open.

    What `db.using(alias)` returns. Everything a database does, it does; what
    it adds is the redirection: for as long as one of its blocks is open, a
    model that lives on the default database resolves here instead.
    """

    def __init__(
        self,
        db: Any,  # noqa: ANN401
        override: ContextVar[str | None],
        alias: str,
    ) -> None:
        self._db = db
        self._override = override
        self._alias = alias

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._alias!r}, {self._db!r})"

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401
        """Everything else is the database's own."""
        return getattr(self._db, name)

    def __enter__(self) -> Any:  # noqa: ANN401
        """Redirect for the block, opening nothing."""
        self._token = self._override.set(self._alias)
        return self._db

    def __exit__(self, *exc_info: object) -> None:
        self._override.reset(self._token)

    def connect(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open a connection here, and redirect while it is open."""
        return self._redirect(self._db.connect(**arguments))

    def transaction(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open a transaction here, and redirect while it is open."""
        return self._redirect(self._db.transaction(**arguments))

    def autocommit(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open an autocommit block here, and redirect while it is open."""
        return self._redirect(self._db.autocommit(**arguments))

    def session_factory(self, **arguments: Any) -> _Redirected:  # noqa: ANN401
        """Open a session here, and redirect while it is open."""
        return self._redirect(self._db.session_factory(**arguments))

    def _redirect(self, block: Any) -> _Redirected:  # noqa: ANN401
        return _Redirected(block, self._override, self._alias)


class _Redirected:
    """A block of another database, with the redirection around it.

    Awaited or not, whichever the block underneath is.
    """

    def __init__(
        self,
        block: Any,  # noqa: ANN401
        override: ContextVar[str | None],
        alias: str,
    ) -> None:
        self._block = block
        self._override = override
        self._alias = alias

    def __enter__(self) -> Any:  # noqa: ANN401
        self._token = self._override.set(self._alias)
        try:
            return self._block.__enter__()
        except BaseException:
            self._override.reset(self._token)
            raise

    def __exit__(self, *exc_info: object) -> Any:  # noqa: ANN401
        try:
            return self._block.__exit__(*exc_info)
        finally:
            self._override.reset(self._token)

    async def __aenter__(self) -> Any:  # noqa: ANN401
        self._token = self._override.set(self._alias)
        try:
            return await self._block.__aenter__()
        except BaseException:
            self._override.reset(self._token)
            raise

    async def __aexit__(self, *exc_info: object) -> Any:  # noqa: ANN401
        try:
            return await self._block.__aexit__(*exc_info)
        finally:
            self._override.reset(self._token)


class _DatabaseRegistryMixin(BaseDatabase[Any, Any], Generic[DatabaseT]):
    """The registry half of the database an application imports.

    Mixed into the concrete `Databases` on either side, which adds the database
    half and the disposal the asyncio one awaits.
    """

    _database_class: type[DatabaseT]

    def __init__(self) -> None:
        """Leave everything to [`configure`][sqlakit.Databases.configure]."""
        self._aliased: dict[str, DatabaseT] = {}
        self._routers: tuple[Any, ...] = ()
        self._using: ContextVar[str | None] = ContextVar(
            f"{type(self).__name__}.using", default=None
        )

    def __repr__(self) -> str:
        if not self.is_configured:
            return f"{type(self).__name__}(unconfigured)"
        return super().__repr__()

    def __getitem__(self, alias: str) -> Self | DatabaseT:
        """Return the database configured as ``alias``.

        ``db["default"]`` is this one: what the code reaches without an alias.

        Raises:
            UnknownDatabaseError: if nothing is configured under that alias.

        """
        if alias == DEFAULT_ALIAS:
            return self
        try:
            return self._aliased[alias]
        except KeyError:
            raise UnknownDatabaseError(alias, self.aliases) from None

    def __contains__(self, alias: str) -> bool:
        return alias == DEFAULT_ALIAS or alias in self._aliased

    @contextmanager
    def recording(
        self,
        label: str | None = None,
        *,
        logger: logging.Logger | None = None,
        echo: bool = False,
        stacks: bool = False,
        into: Recording | None = None,
    ) -> Iterator[Recording]:
        """Record every database this registry has, not the default one alone.

        ```python
        with db.recording() as record:
            move_the_reports()

        record.databases  # ("default", "warehouse")
        ```

        Statements say which database ran them. `db["warehouse"].recording()` records
        that one on its own.
        """
        together = Recording(label=label) if into is None else into
        databases = (self, *self._aliased.values())
        with ExitStack() as stack:
            for db in databases:
                stack.enter_context(
                    BaseDatabase.recording(db, label, stacks=stacks, into=together)
                )
            try:
                yield together
            finally:
                if logger is not None:
                    together.log(logger)
                if echo:
                    together.echo()

    @staticmethod
    def _named(alias: str, db: DatabaseT) -> DatabaseT:
        """Let a database say which alias it answers to, when it is recorded."""
        db._name = alias  # noqa: SLF001
        return db

    def using(self, alias: str) -> _Using:
        """Return the database under that alias, standing in for the default one.

        The block opens on it, and models that live on the default database resolve
        there for as long as it is open:

        ```python
        with db.using("replica").connect():
            report = build_report()
        ```

        Models that live somewhere else stay where they are, and a query that named
        its own database with `using()` still wins. Entered on its own, as `with
        db.using("replica"):`, it redirects and opens nothing.

        Raises:
            UnknownDatabaseError: if nothing is configured under that alias.

        """
        if alias not in self:
            raise UnknownDatabaseError(alias, self.aliases)
        return _Using(self[alias], self._using, alias)

    def route(self, *routers: Router | RouterFunction | str) -> None:
        """Say which database a model lives on, for models that do not say it.

        Each router takes a model and returns an alias, or None to leave the question
        to the next one. A dotted path is imported, which is what settings hand over:

        ```python
        db.route(lambda model: "warehouse" if is_report(model) else None)
        db.route("app.db.routing")
        ```

        Placement is structural: reads, writes and the tables `provisioned_tables()`
        creates all follow it. Called with nothing, it clears the policy, which
        leaves `__db__` on a model as the only answer.
        """
        self._routers = tuple(
            as_router(import_string(router) if isinstance(router, str) else router)
            for router in routers
        )

    @property
    def routers(self) -> tuple[Any, ...]:
        """The placement policy in force, in the order it is asked."""
        return self._routers

    def db_for(self, model: type[Any]) -> BaseDatabase[Any, Any]:
        """Return the database a model lives on.

        The routers first, then the model's own ``__db__``, unless a block
        opened with `using()` stands in for the default database.
        """
        placement = self._routed(model) or model.__db__
        if isinstance(placement, str):
            override = self._using.get()
            if override is not None and placement == DEFAULT_ALIAS:
                placement = override
            return self[placement]
        return placement

    def _routed(self, model: type[Any]) -> str | None:
        """Return what the first router says about this model, if anything."""
        for router in self._routers:
            alias = router(model)
            if alias is not None:
                return alias
        return None

    @property
    def aliases(self) -> tuple[str, ...]:
        """The aliases configured, the default one first."""
        return (DEFAULT_ALIAS, *self._aliased)

    @property
    def is_configured(self) -> bool:
        """Whether [`configure`][sqlakit.Databases.configure] has been called."""
        return "url" in self.__dict__

    @overload
    def configure(
        self,
        url: str | sa.URL | None = None,
        engine_args: EngineArgs | None = None,
        session_args: SessionArgs | None = None,
        routers: Sequence[Router | RouterFunction | str] = (),
        templates: TemplatesLike | None = None,
        **parts: Unpack[UrlParts],
    ) -> None: ...

    @overload
    def configure(
        self,
        url: Mapping[str, DatabaseConfig],
        *,
        routers: Sequence[Router | RouterFunction | str] = (),
        templates: TemplatesLike | None = None,
    ) -> None: ...

    def configure(
        self,
        url: str | sa.URL | Mapping[str, DatabaseConfig] | None = None,
        engine_args: EngineArgs | None = None,
        session_args: SessionArgs | None = None,
        routers: Sequence[Router | RouterFunction | str] = (),
        templates: TemplatesLike | None = None,
        **parts: Unpack[UrlParts],
    ) -> None:
        """Point this database at ``url``, or at several keyed by alias.

        Call it once, at startup. Settings arrive as a URL or as the parts to build
        one from:

        ```python
        db.configure(DB_URL)

        db.configure(
            drivername="postgresql+psycopg",
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
        )
        ```

        A mapping configures this database as its ``"default"`` and builds the rest
        alongside it, each with its own pool, connection and transactions:

        ```python
        db.configure(
            {
                "default": {"url": PRIMARY_URL, "engine_args": {"pool_size": 20}},
                "replica": {"url": REPLICA_URL},
            }
        )

        db["replica"].session  # the replica; `db.session` is the default one
        ```

        ``routers`` says where a model lives when the model does not, as `route`
        takes them. ``templates`` is where the SQL templates of every one of them
        live. Reconfiguring is allowed until something connects; afterwards, dispose
        of the engines first.

        Raises:
            DatabaseAlreadyConfiguredError: if a database has already connected.
            MissingDefaultDatabaseError: if a mapping carries no ``"default"``.
            MissingDatabaseUrlError: if an entry says nowhere to connect.
            ConflictingDatabaseUrlError: if one says it twice over. Either is an
                ``InvalidDatabaseConfigError``.

        """
        if not isinstance(url, Mapping):
            self._reject_if_connected()
            super().__init__(url, engine_args, session_args, templates, **parts)
            self.route(*routers)
            return
        if DEFAULT_ALIAS not in url:
            raise MissingDefaultDatabaseError(tuple(url))
        self._reject_if_connected()
        configs = {
            alias: (url_from_config(config), config) for alias, config in url.items()
        }
        default_url, default = configs[DEFAULT_ALIAS]
        super().__init__(
            default_url,
            default.get("engine_args"),
            default.get("session_args"),
            templates,
        )
        self._aliased = {
            alias: self._named(
                alias,
                self._database_class(
                    database_url,
                    config.get("engine_args"),
                    config.get("session_args"),
                    templates,
                ),
            )
            for alias, (database_url, config) in configs.items()
            if alias != DEFAULT_ALIAS
        }
        self.route(*routers)

    def _reject_if_connected(self) -> None:
        connected = self.is_configured and self._engine is not None
        if connected or any(
            db._engine is not None  # noqa: SLF001
            for db in self._aliased.values()
        ):
            raise DatabaseAlreadyConfiguredError

    if not TYPE_CHECKING:
        # Hidden from type checkers: seeing it, they would take every attribute
        # to exist and stop reporting typos. It is reached when normal lookup
        # fails, which is what an unconfigured database looks like.
        def __getattr__(self, name: str) -> object:
            # Only the database's own attributes are worth explaining. Anything
            # else is a name that does not exist, and saying so is what lets
            # `hasattr`, `copy` and every library that introspects work.
            if (
                not name.startswith("_")
                and hasattr(type(self), name)
                and not self.is_configured
            ):
                raise DatabaseNotConfiguredError from None
            raise AttributeError(name)


_CONTROL = frozenset(
    # Transaction control is not a query, and which of these reach a cursor
    # depends on the driver, and counting them would make a recording mean
    # something different on SQLite than on PostgreSQL.
    {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "PRAGMA"}
)

URL_PARTS = (
    "drivername",
    "username",
    "password",
    "host",
    "port",
    "database",
    "query",
)
"""The parts a configuration is spelled with instead of a ``url``."""


def url_from_config(config: DatabaseConfig) -> str | sa.URL:
    """Read the URL out of a configuration, however it was spelled.

    Raises:
        MissingDatabaseUrlError: if given neither a ``url`` nor the parts to build
            one.
        ConflictingDatabaseUrlError: if given both.

    """
    parts = {part: config[part] for part in URL_PARTS if part in config}
    if "url" in config:
        if parts:
            raise ConflictingDatabaseUrlError(tuple(parts))
        return config["url"]
    if "drivername" not in parts:
        raise MissingDatabaseUrlError
    return sa.URL.create(**parts)  # ty: ignore[invalid-argument-type]


class _LazyBind:
    """A session that checks its connection out on first real use.

    Mixed over the session class of a lazy ``session_factory()`` block. The
    session is created unbound, and ``get_bind`` performs the block's checkout
    the first time the session needs a connection: on a flush or a query, not
    on ``add()``.
    """

    _sqlakit_checkout: Callable[[], Any] | None = None
    bind: Any

    def get_bind(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        if self.bind is None and self._sqlakit_checkout is not None:
            self.bind = self._sqlakit_checkout()
        return super().get_bind(*args, **kwargs)  # ty: ignore[unresolved-attribute]


@cache
def lazy_session_class(base: type) -> type:
    """Return ``base`` with `_LazyBind` mixed in, built once per base."""
    if issubclass(base, _LazyBind):
        return base
    return type(f"Lazy{base.__name__}", (_LazyBind, base), {})


class BaseRetryingTransaction:
    """What ``transaction()`` returns when given ``retry_on``.

    A decorator, and deliberately not a context manager: retrying re-runs the
    block, which a ``with`` statement cannot do. Entering one is rejected by
    type checkers and raises at runtime.
    """

    def __init__(
        self,
        transaction: Callable[[], Any],
        *,
        retry_on: RetryOn,
        max_retries: int = 3,
        backoff: Callable[[int], float] | None = None,
    ) -> None:
        self.transaction = transaction
        self.retry_on = retry_on
        self.max_retries = max_retries
        self.backoff = backoff or default_backoff

    def _retry(self, exc: BaseException, *, attempt: int) -> bool:
        return attempt < self.max_retries and retry_matches(exc, self.retry_on)

    if not TYPE_CHECKING:
        # Hidden from type checkers, which reject `with` on this class outright.
        # Defined for the error message.
        def __enter__(self) -> None:
            raise RetryNotSupportedError

        def __exit__(self, *exc_info: object) -> None:
            raise AssertionError  # pragma: no cover

        async def __aenter__(self) -> None:
            raise RetryNotSupportedError

        async def __aexit__(self, *exc_info: object) -> None:
            raise AssertionError  # pragma: no cover


def retry_matches(exc: BaseException, retry_on: RetryOn) -> bool:
    """Whether ``retry_on`` claims this exception is worth another attempt."""
    if isinstance(retry_on, type | tuple):
        return isinstance(exc, retry_on)
    return retry_on(exc)


def default_backoff(attempt: int) -> float:
    """Exponential backoff with jitter: ~0.1s, ~0.2s, ~0.4s, ..."""
    return 0.1 * (2**attempt) * (0.5 + _random.random())


def fix_sqlite_transactions(engine: Engine) -> None:
    """Make the stdlib SQLite driver emit real transactions.

    ``sqlite3`` never emits ``BEGIN`` on its own, which leaves ``SAVEPOINT``
    and nested transactions broken. The workaround is SQLAlchemy's, and covers
    ``pysqlite`` and ``aiosqlite`` alike.
    """
    if engine.dialect.name != "sqlite":
        return

    @sa.event.listens_for(engine, "connect")
    def disable_implicit_begin(dbapi_connection: Any, _record: object) -> None:  # noqa: ANN401
        dbapi_connection.isolation_level = None

    @sa.event.listens_for(engine, "begin")
    def emit_begin(connection: sa.Connection) -> None:
        # SQLAlchemy signals a begin under AUTOCOMMIT too, where a real
        # transaction would undo what AUTOCOMMIT was asked for.
        if _is_autocommit(connection):
            return
        connection.exec_driver_sql("BEGIN")


def _is_autocommit(connection: sa.Connection) -> bool:
    """Whether ``connection`` runs in ``AUTOCOMMIT``, per block or per engine."""
    if connection.get_execution_options().get("isolation_level") == "AUTOCOMMIT":
        return True
    return getattr(connection.dialect, "_on_connect_isolation_level", None) == (
        "AUTOCOMMIT"
    )
