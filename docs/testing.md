# Testing

Tests run against a real database. Which one is up to you: `SQLite` is fast,
needs nothing installed, and supports everything this page uses, savepoints
included. If your code depends on the dialect, its types, its constraints or
its migrations, test on the database you deploy on. Many suites do both, and
this page works the same either way.

!!! note "Why savepoints work on `SQLite`"

    The `SQLite` driver in the standard library does not issue `BEGIN` on its
    own, which breaks `SAVEPOINT` and nested transactions. `Database` applies
    the workaround from `SQLAlchemy`'s pysqlite documentation for
    `sqlite+pysqlite` and `sqlite+aiosqlite`, and skips it under `AUTOCOMMIT`,
    where opening a transaction would defeat the purpose of the mode.

Running against a database works because of two guarantees: every test leaves
the database as it found it, and the code under test runs the same way it does
in production.

`provisioned_tables()` and `transaction(rollback=True)` provide both:

- **`provisioned_tables()`** creates the schema once for the whole run and drops
  it afterwards.
- **`transaction(rollback=True)`** wraps each test in a real transaction and
  rolls it back at the end. Everything inside runs on one connection and cannot
  commit past it: both the functions the test calls and the blocks those
  functions open.

Nested blocks join the enclosing one by default, so a single `rollback=True` is
enough. `join_nested=True` is already the default, and `join_nested=False`
moves the code under test off the test's connection, where it commits for real
and the rollback cannot undo it.

## The database a test needs

```python title="conftest.py"
from collections.abc import Iterator

import pytest

from app.db import db
from app.models import Model


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: the test needs a database")


@pytest.fixture(scope="session")
def _create_db() -> Iterator[None]:
    with Model.provisioned_tables():
        yield


@pytest.fixture(autouse=True)
def _db_marker(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("db") is None:
        yield
        return

    request.getfixturevalue("_create_db")
    with db.transaction(rollback=True):
        yield
```

```python
@pytest.mark.db
def test_renaming_a_user() -> None:
    user = User(name="ada").save()

    rename(user.id, "grace")

    user.refresh()

    assert user.name == "grace"
```

`refresh()` discards what the session cached for the object and reads the row
from the database again. Without it your assertion checks nothing: the code
under test ran on the same session as the test, so `user` is the same instance
it changed, and a fresh query would return that same object. The test would
pass even if nothing had reached the database.

`_db_marker` runs for every test, but returns immediately when the marker is
absent, so a test that doesn't need a database opens no connection. When no
test is marked, no schema is built either, because `_create_db` is requested
only inside that branch.

## With await

Switching to an async database changes one thing: the fixtures have to be
async, because the transaction has to open on the loop the test runs on. An
async fixture can't be fetched through `getfixturevalue`: that call is
synchronous and can't await anything. So the `db` marker is handled at
collection time, in `pytest_collection_modifyitems`, which adds the fixture
to the marked tests:

```python title="conftest.py (asyncio)"
from collections.abc import AsyncIterator

import pytest

from app.db import db
from app.models import Model


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: the test needs a database")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give a database to the marked tests and to nothing else."""
    for item in items:
        if isinstance(item, pytest.Function) and item.get_closest_marker("db"):
            item.fixturenames.append("_db_marker")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def _create_db() -> AsyncIterator[None]:
    async with Model.provisioned_tables():
        yield


@pytest.fixture
async def _db_marker(_create_db: None) -> AsyncIterator[None]:
    async with db.transaction(rollback=True):
        yield
```

`pytest` reads `item.fixturenames` at that moment. A `usefixtures` marker added
this late is ignored.

Mark your tests as `anyio`, which you'd be doing for async tests anyway:

```python
@pytest.mark.anyio
@pytest.mark.db
async def test_renaming_a_user() -> None:
    user = await User(name="ada").save()

    await rename(user.id, "grace")

    await user.refresh()

    assert user.name == "grace"
```

## Tables missing from the schema

`provisioned_tables()` creates the tables the metadata holds. A model whose
module was never imported is not in the metadata, so its tables are missing,
and the failure looks like a bug in the test. If your application keeps models
next to the features they belong to, import them all first:

```python
from sqlakit import import_models


@pytest.fixture(scope="session")
def _create_db() -> Iterator[None]:
    import_models("app")
    with Model.provisioned_tables():
        yield
```

## A schema without the model layer

If your metadata doesn't come from the model layer, the same
`provisioned_tables` is available on the database:

```python
with db.provisioned_tables(SQLModel.metadata):
    yield
```

## More than one database

Pass the alias, and each database gets the tables of the models that point at
it. An association table is created on the same database as the rows it joins:

```python
@pytest.fixture(scope="session")
def _create_db() -> Iterator[None]:
    with Model.provisioned_tables(), Model.provisioned_tables("warehouse"):
        yield
```

Then open a transaction on each one. `transactions()` does that for every
database in the registry:

```python
@pytest.fixture(autouse=True)
def _db_marker(request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("db") is None:
        yield
        return

    request.getfixturevalue("_create_db")
    with db.transactions(rollback=True):
        yield
```

Your test can now write to either database through its model, and both
transactions roll back when it ends:

```python
@pytest.mark.db
def test_a_signup_is_recorded() -> None:
    register_user("ada@example.com")

    assert User.query.count() == 1
    assert Event.query.count() == 1  # `Event.__db__` is "warehouse"
```

### What a rollback on every database does not do

Each database rolls back its own transaction, on its own connection. Two
consequences follow:

- A test that writes to one database and reads from another sees only what the
  second one holds. No transaction spans both, and no isolation level changes
  that.
- A replica alias is a second connection, and it cannot see the test's
  uncommitted rows even when it points at the same database.

The second point breaks the suite as soon as a router appears in the project:
reads go to `replica`, the test's rows are never committed, and every read
comes back empty. Keep routers off in tests. A fresh registry has none, and
the fixture below clears them after each test:

```python
@pytest.fixture(autouse=True)
def _db_marker() -> Iterator[None]:
    with db.transactions(rollback=True):
        yield
    db.route()  # no routers, reads and writes both go to default
```

A model that set its database through `__db__` keeps it: `route()` clears the
routing policy, not the model's own setting. Test the policy by checking where
a model resolves, not by reading data:

```python
def test_reads_go_to_the_replica() -> None:
    db.route(reads_go_to_the_replica)

    assert User.db is db["replica"]
```

### Counting queries across both

The standalone `assert_queries` watches every database in the registry, so a
block that touches two databases gets one combined count. To watch a single
one, pass its alias or the database itself:

```python
from sqlakit.testing import assert_queries

with assert_queries(2):
    User.query.count()
    Event.query.count()

with assert_queries(1, using="warehouse"):
    build_report()
```

`db.assert_queries` watches one database; on the registry it covers them all.

A recording tracks which database ran each statement, so a test can prove that
nothing reached the warehouse:

```python
with db.recording() as record:
    register_user("ada@example.com")

assert record.databases == ("default",)
```

## Migrations instead of `create_all`

If your application has migrations, test the schema you'll actually deploy:
run the migrations once per session, and keep the rollback around each test as
it is.

If you don't pass the test's connection to `Alembic`, it opens one of its own
and the migration runs outside your transaction. The rollback can't undo it,
and the schema outlives the run. So your `env.py` needs to accept a connection
from outside; the rest of this section depends on it:

```python title="migrations/env.py"
from alembic import context

connection = context.config.attributes.get("connection")

if connection is not None:
    # Handed in from outside: by the tests, or by a script.
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = sa.create_engine(DATABASE_URL, poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
```

`connection` is the same name on both sides: the key the fixtures below put
into `config.attributes`, and the argument of `context.configure`.

Two fixtures follow: one reads `alembic.ini`, the other passes the connection.
`Alembic` resolves `script_location` from the working directory, so build the
config from the project root and stay there while it runs. Otherwise your tests
pass when run from the root and fail from anywhere else.

```python
import contextlib
from collections.abc import Iterator

import alembic.command
import alembic.config
import pytest


@pytest.fixture(scope="session")
def alembic_config(pytestconfig: pytest.Config) -> Iterator[alembic.config.Config]:
    with contextlib.chdir(pytestconfig.rootpath):
        yield alembic.config.Config(pytestconfig.rootpath / "alembic.ini")


@pytest.fixture(scope="session")
def _create_db(alembic_config: alembic.config.Config) -> Iterator[None]:
    with db.transaction() as conn:
        alembic_config.attributes["connection"] = conn
        alembic.command.upgrade(alembic_config, "head")
    yield
    with db.transaction() as conn:
        alembic_config.attributes["connection"] = conn
        alembic.command.downgrade(alembic_config, "base")
```

`Alembic` is synchronous, so on an async database the migrations run through
`run_sync`:

```python
@pytest.fixture(scope="session")
async def _create_db(alembic_config: alembic.config.Config) -> AsyncIterator[None]:
    def upgrade(connection: sa.Connection) -> None:
        alembic_config.attributes["connection"] = connection
        alembic.command.upgrade(alembic_config, "head")

    def downgrade(connection: sa.Connection) -> None:
        alembic_config.attributes["connection"] = connection
        alembic.command.downgrade(alembic_config, "base")

    async with db.transaction() as conn:
        await conn.run_sync(upgrade)
    yield
    async with db.transaction() as conn:
        await conn.run_sync(downgrade)
```

`run_sync` passes the function the underlying synchronous connection.
`Alembic` requires a synchronous one, and `env.py` reads it as shown above.

## Counting queries

`assert_queries` is a [recorder](debugging.md) with an assertion around it. It
counts the same way and prints the same statements on failure. It lives on the
database, next to `recording()`:

```python
@pytest.mark.db
def test_the_list_page_costs_two_queries() -> None:
    with db.assert_queries(2):
        User.query.order_by("name").page(limit=10)
```

The checks work alone or combined: an exact number, an `at_most` ceiling, and
`duplicates=False` to forbid repeats, which catches an N+1.

```python
with db.assert_queries(at_most=5):
    ...  # an upper bound instead of an exact number
with db.assert_queries(duplicates=False):
    ...  # no repeats allowed, catches N+1
with db.assert_queries(3, duplicates=False):
    ...
```

`db.assert_queries` watches the database it was called on. To watch several at
once, or the configured registry, use the function of the same name from
`sqlakit.testing`:

```python
from sqlakit.testing import assert_queries

with assert_queries(2):  # every database `sqlakit.db` has
    ...
with assert_queries(2, using="warehouse"):  # one of them, by alias
    ...
```

On failure it prints everything that ran, with repeats cross-referenced:

```sql
AssertionError: 4 queries, expected 2

   1    0.4ms  SELECT players.id, players.team_id FROM players ORDER BY players.id ASC
   2    0.2ms  SELECT teams.id, teams.name FROM teams WHERE teams.id = ?
              ↑ same as 3, 4 — 3 times in all
   3    0.2ms  SELECT teams.id, teams.name FROM teams WHERE teams.id = ?
              ↑ same as 2, 4 — 3 times in all
   4    0.2ms  SELECT teams.id, teams.name FROM teams WHERE teams.id = ?
              ↑ same as 2, 3 — 3 times in all
```

It needs two things in place:

- **A database to watch.** `db.assert_queries` takes the one it was called on.
  The standalone function takes the whole registry, unless `using` names a
  single database.
- **A test that has a database at all**, meaning the `db` marker above it.
  Without the marker nothing is connected, and the code under test raises
  instead of counting zero.

The block is a plain `with` in both cases, under `asyncio` as well, because
recording only listens and runs nothing itself. When you want the numbers
themselves rather than an assertion, use `db.recording()`, the recorder this
is built on.

## Reading what the code changed

The code under test writes on the test's connection, so the rows are already
in the database. The instance your test holds, though, still carries the
values it was loaded with. Read the row again:

```python
@pytest.mark.db
def test_revoking_a_token(token: Token) -> None:
    revoke(token.id)

    token.refresh()

    assert token.is_revoked
```

## Seeding data

A fixture that writes data is like any other: it runs inside the test's
transaction and rolls back with it.

```python
@pytest.fixture
def team() -> Team:
    return Team(name="red").save()
```

## What the test's block changes

A rolled-back block is an ordinary transaction, so the code under test behaves
as it does in production. Inside it:

- A nested `transaction()` joins the test's transaction on the same connection
  instead of opening its own. Its commit only flushes, and only the test's
  block commits or rolls back for real.
- `autocommit()` joins as well and commits nothing inside the block: it uses
  the connection the test bound, so everything written rolls back with the
  rest. It opens an `AUTOCOMMIT` connection of its own only when no connection
  is bound.
- A nested `transaction(rollback=True)` takes a savepoint, so it undoes its own
  writes and leaves the test's alone.
- A session that rolls *itself* back, through `session_factory()` and then
  `rollback()`, ends the whole transaction. The block then raises
  `TransactionRolledBackError`, because there is nothing left to commit.
  Production behaves the same way; a block that should fail on its own needs
  `transaction(savepoint=True)`.

The point of all this is that your tests behave the way production does.

Next: [debugging](debugging.md), for watching the same queries outside a test.
