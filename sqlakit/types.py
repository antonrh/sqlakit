from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    import sqlalchemy as sa
    from sqlalchemy.engine import Connection, Engine
    from sqlalchemy.orm import Query, Session
    from sqlalchemy.pool import Pool

    from ._sql import Templates

__all__ = [
    "DatabaseConfig",
    "EngineArgs",
    "SessionArgs",
    "TemplatesLike",
    "UrlParts",
]

# Quoted, so importing this module never reaches `Templates` and the
# `jinja2sql` behind it.
TemplatesLike: TypeAlias = "str | Path | Sequence[str | Path] | Templates"
"""Where a database's SQL templates are: a path, several, or the object."""


class EngineArgs(TypedDict, total=False):
    """Keyword arguments accepted by [`sqlalchemy.create_engine`](https://docs.sqlalchemy.org/en/20/core/engines.html#sqlalchemy.create_engine)."""

    connect_args: dict[str, Any]
    echo: bool | Literal["debug"]
    echo_pool: bool | Literal["debug"]
    enable_from_linting: bool
    execution_options: dict[str, Any]
    hide_parameters: bool
    insertmanyvalues_page_size: int
    isolation_level: str
    json_deserializer: Callable[[str], Any]
    json_serializer: Callable[[Any], str]
    label_length: int | None
    logging_name: str
    max_identifier_length: int | None
    max_overflow: int
    module: Any
    paramstyle: Literal["qmark", "numeric", "named", "format", "pyformat"]
    plugins: list[str]
    pool: Pool
    pool_logging_name: str
    pool_pre_ping: bool
    pool_recycle: int
    pool_reset_on_return: Literal["rollback", "commit"] | None
    pool_size: int
    pool_timeout: float
    pool_use_lifo: bool
    poolclass: type[Pool]
    query_cache_size: int
    skip_autocommit_rollback: bool
    use_insertmanyvalues: bool


class SessionArgs(TypedDict, total=False):
    """Keyword arguments accepted by `sqlalchemy.orm.sessionmaker`.

    No ``bind``: sessions bind to the connection of the surrounding block.
    """

    autobegin: bool
    autoflush: bool
    binds: dict[Any, Engine | Connection]
    class_: type[Session]
    enable_baked_queries: bool
    expire_on_commit: bool
    info: dict[Any, Any]
    join_transaction_mode: Literal[
        "conditional_savepoint",
        "rollback_only",
        "control_fully",
        "create_savepoint",
    ]
    query_cls: type[Query[Any]]
    twophase: bool


class UrlParts(TypedDict, total=False):
    """A database URL spelled out, as `sqlalchemy.URL.create` takes it."""

    drivername: str
    username: str | None
    password: str | None
    host: str | None
    port: int | None
    database: str | None
    query: Mapping[str, Sequence[str] | str]


class QueryStats(TypedDict):
    """The numbers a recording adds up to, for a log read by machine."""

    queries: int
    milliseconds: float
    slowest_milliseconds: float
    duplicated: int
    databases: tuple[str, ...]
    label: str | None


class DatabaseConfig(UrlParts, total=False):
    """One database in a configuration keyed by alias.

    Give it a ``url`` or the parts to build one from, never both.
    """

    url: str | sa.URL
    engine_args: EngineArgs
    session_args: SessionArgs
