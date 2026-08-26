# Reference

## Blocks

| | opens | commits | inside another block |
| --- | --- | --- | --- |
| `db.connect()` | a connection | no | reuses the bound connection |
| `db.transaction()` | a connection and a transaction | on the way out | joins the transaction |
| `db.autocommit()` | a connection in `AUTOCOMMIT` | every statement | joins the transaction |
| `db.session_factory()` | a connection and a session | no | reuses the bound connection |

## Transaction arguments {#transaction-arguments}

| | default | |
| --- | --- | --- |
| `savepoint` | `False` | Run a nested block as a savepoint. Applies to the blocks under it as well. |
| `join_nested` | `True` | Blocks under this one reuse its connection. |
| `rollback` | `False` | Roll back instead of committing. Implies `savepoint`. |
| `commit_on_error` | `None` | Exception types that commit anyway. |
| `retry_on` | `None` | Exception types or a predicate worth retrying for. Works as a decorator only, and only on the block that owns the transaction. |
| `max_retries` | `3` | How many extra attempts `retry_on` gets. |
| `backoff` | exponential with jitter | Seconds to wait before attempt `n`, counting from zero. |

## Defaults {#defaults}

These sit under whatever you pass in `engine_args` and `session_args`.

| | default | why |
| --- | --- | --- |
| `pool_pre_ping` | `True` | A connection dropped by the server, a proxy or a failover turns into a reconnect rather than an error mid-statement. |
| `pool_recycle` | `1800` | Reopen a connection before something else closes it: `MySQL`'s eight-hour `wait_timeout`, `PgBouncer` and cloud balancers all cut idle ones. |
| `expire_on_commit` | `False` | Attributes stay readable after a commit. With expiry on, a read after a commit issues a lazy `SELECT`, which under `asyncio` fails with `MissingGreenlet`. |

`pool_size`, `max_overflow` and `isolation_level` are left alone, since they
depend on the worker count, the database's limit and the dialect.

## The async API {#async}

`sqlakit.asyncio` mirrors `sqlakit`. Reaching the database is awaited, reading
the context is not.

| synchronous | `asyncio` |
| --- | --- |
| `with db.connect():` | `async with db.connect():` |
| `with db.transaction():` | `async with db.transaction():` |
| `with db.autocommit():` | `async with db.autocommit():` |
| `db.dispose()` / `db.ping()` | `await db.dispose()` / `await db.ping()` |
| `user.save()` / `user.delete()` | `await user.save()` / `await user.delete()` |
| `user.merge()` / `user.refresh()` | `await user.merge()` / `await user.refresh()` |
| `User.query.get(id)` / `.get_one(id)` | `await User.query.get(id)` / `.get_one(id)` |
| `User.query.page(...)` / `.cursor_page(...)` | `await User.query.page(...)` / `.cursor_page(...)` |
| `db.sql(...).all()` | `await db.sql(...).all()` |
| `query.where(...)`, `query.order_by(...)` | the same, unawaited |
| `db.connection`, `db.session`, `db.in_transaction()` | the same |

`SQLAlchemy`'s async drivers want a running `asyncio` loop, so `trio` will not
do.

## Database

::: sqlakit.Database
    options:
      inherited_members: true

## The registry

::: sqlakit.Databases

::: sqlakit.db

::: sqlakit.Router
    options:
      inherited_members: true

### Defaults

::: sqlakit.DEFAULT_ENGINE_ARGS

::: sqlakit.DEFAULT_SESSION_ARGS

::: sqlakit.DEFAULT_ALIAS

## Transaction

::: sqlakit.Transaction

::: sqlakit.RetryingTransaction
    options:
      inherited_members: true

## Model

Everything a model can do lives on the mixin, and `Model` is that mixin on a
plain declarative base.

::: sqlakit.orm.Model

::: sqlakit.orm.ModelMixin
    options:
      inherited_members: true

::: sqlakit.orm.SoftDeletes

## Query

::: sqlakit.orm.Query
    options:
      inherited_members: true

::: sqlakit.orm.ColumnQuery

::: sqlakit.orm.QueryDescriptor

::: sqlakit.import_models

::: sqlakit.import_string

::: sqlakit.testing.assert_queries

::: sqlakit.Recording

::: sqlakit.Statement

::: sqlakit.QueryStats

::: sqlakit.Page

::: sqlakit.CursorPage

::: sqlakit.OrderBy

## SQL templates

Behind the `sqlakit[sql]` extra, covered in [SQL templates](sql.md).

::: sqlakit.sql.SQL

::: sqlakit.sql.SQLQuery
    options:
      inherited_members: true

::: sqlakit.sql.SQLRows

::: sqlakit.sql.Templates

## The async classes

The same classes, awaited.

::: sqlakit.asyncio.Database
    options:
      inherited_members: true

::: sqlakit.asyncio.Databases
    options:
      inherited_members: true

::: sqlakit.asyncio.db

::: sqlakit.asyncio.Transaction

::: sqlakit.asyncio.RetryingTransaction
    options:
      inherited_members: true

::: sqlakit.asyncio.orm.ModelMixin
    options:
      inherited_members: true

::: sqlakit.asyncio.orm.SoftDeletes

::: sqlakit.asyncio.orm.Query
    options:
      inherited_members: true

::: sqlakit.asyncio.orm.ColumnQuery

::: sqlakit.asyncio.orm.QueryDescriptor

::: sqlakit.asyncio.sql.SQL

::: sqlakit.asyncio.sql.SQLQuery
    options:
      inherited_members: true

::: sqlakit.asyncio.sql.SQLRows

## Arguments

::: sqlakit.EngineArgs

::: sqlakit.SessionArgs

::: sqlakit.DatabaseConfig

::: sqlakit.UrlParts

## Exceptions

Every one of them inherits `SQLAKitError`, and a builtin exception on top, which
says what kind of thing went wrong. That second base is a convenient way to sort
the answers an API gives:

| also a | what happened | the usual answer |
| --- | --- | --- |
| `ValueError` | something invalid arrived: an ordering field nobody offered, or a cursor that will not decode | 400 |
| `TypeError` | the code asked for the impossible: a page with no ordering, or `get` on a narrowed query | 500 and a fix |
| `RuntimeError` | the block is in the wrong state: no connection, no session, or a transaction already rolled back | 500 |
| `KeyError` and `SQLAlchemy`'s exceptions | what the name says, for code that already catches them: a missing row is a `NoResultFound` as well | |

```python
import sqlakit

try:
    page = User.query.order_by(sort).page(limit=20)
except sqlakit.UnknownOrderFieldError:  # this field, this request
    ...
except sqlakit.SQLAKitError:  # anything from this library
    ...
except ValueError:  # any bad input, from here or elsewhere
    ...
```

::: sqlakit.SQLAKitError

### Blocks

::: sqlakit.MissingConnectionError

::: sqlakit.MissingSessionError

::: sqlakit.TransactionRolledBackError

::: sqlakit.RetryNotSupportedError

::: sqlakit.DetachedInstanceError

### Configuration

::: sqlakit.DatabaseNotConfiguredError

::: sqlakit.DatabaseAlreadyConfiguredError

::: sqlakit.UnknownDatabaseError

::: sqlakit.MissingDefaultDatabaseError

::: sqlakit.InvalidDatabaseConfigError

::: sqlakit.MissingDatabaseUrlError

::: sqlakit.ConflictingDatabaseUrlError

::: sqlakit.UnknownImportPathError

::: sqlakit.MissingDependencyError

### Ordering and pagination

::: sqlakit.UnorderedPageError

::: sqlakit.UnknownOrderFieldError

::: sqlakit.InvalidOrderFieldError

::: sqlakit.UncomparableOrderingError

::: sqlakit.InvalidCursorError

::: sqlakit.NullCursorValueError

### Rows and queries

::: sqlakit.InstanceNotFoundError

::: sqlakit.MultipleInstancesFoundError

::: sqlakit.KeyLookupError

::: sqlakit.UnknownFieldError

::: sqlakit.BulkQueryError

::: sqlakit.RawStatementError

### SQL templates

::: sqlakit.SQLNotConfiguredError

::: sqlakit.TemplateNotFoundError

::: sqlakit.StrayParameterError

::: sqlakit.AsyncFilterError
