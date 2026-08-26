# Testing

Tests run against a real database, and which one is your call. `SQLite` is fast,
needs nothing installed, and does everything this page uses, savepoints
included. If your code depends on the dialect, its types, its constraints or its
migrations, test on what you deploy on. Plenty of suites do both, and this page
reads the same either way.

!!! note "Why savepoints work on `SQLite`"

    The `SQLite` driver in the standard library issues no `BEGIN` of its own,
    which breaks `SAVEPOINT` and nested transactions. `Database` applies the
    workaround from `SQLAlchemy`'s pysqlite documentation for `sqlite+pysqlite`
    and `sqlite+aiosqlite`, and skips it under `AUTOCOMMIT`, where a real
    transaction would defeat the point.

A run against a database is bearable for two reasons: every test hands it back
as it found it, and the code under test has no idea it is being tested.

`provisioned_tables()` and `transaction(rollback=True)` deliver both:

- **`provisioned_tables()`** creates the schema once for the whole run and drops
  it afterwards.
- **`transaction(rollback=True)`** wraps each test in a real transaction and
  rolls it back at the end. Everything inside runs on one connection and cannot
  commit around it, both the functions the test calls and the blocks those
  functions open.

Nested blocks join the one they sit inside, and that happens by default, so one
`rollback=True` is all a test needs. `join_nested=True` repeats what is already
there, and `join_nested=False` takes the code under test off the test's
connection, where it commits for real and the rollback can no longer reach it.

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

`refresh()` drops what the session remembers about the object and reads the row
from the database. Without it the assertion checks nothing. The code under test
ran on the same session as the test, so `user` stayed the very instance it
changed, and a fresh query would hand back that same object. The test would pass
even if nothing had reached the database.

`_db_marker` runs for every test, but steps aside at once without the marker, so
a test that wants no database opens no connection. When there are no marked
tests at all, no schema is built either, since `_create_db` is asked for inside
that branch.

## With await

An async database changes one thing, and only because it has to. The fixtures
have to be async, because the transaction has to be opened on the loop the test
runs on. And an async fixture cannot be fetched through `getfixturevalue`: the
call is synchronous, with nothing to await it with. So the `db` marker is caught
at collection, in `pytest_collection_modifyitems`, which is also where the
fixture is put in place, leaving the fixture itself to do only its own work:

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

That moment is where `pytest` reads `item.fixturenames`. A `usefixtures` marker
added this late is ignored.

Mark the tests as `anyio`, which you would be doing anyway:

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

`provisioned_tables()` creates what the metadata holds, and a model whose module
nobody imported is not in there. The tables are missing, and the breakage looks
like a bug in the test. An application that lays models out by feature imports
them first:

```python
from sqlakit import import_models


@pytest.fixture(scope="session")
def _create_db() -> Iterator[None]:
    import_models("app")
    with Model.provisioned_tables():
        yield
```

## A schema without the model layer

For metadata that belongs to no model, the same `provisioned_tables` sits on the
database:

```python
with db.provisioned_tables(SQLModel.metadata):
    yield
```

## More than one database

Name the alias, and each database gets the tables of the models that point at
it. An association table goes where the rows it joins live:

```python
@pytest.fixture(scope="session")
def _create_db() -> Iterator[None]:
    with Model.provisioned_tables(), Model.provisioned_tables("warehouse"):
        yield
```

Then open a transaction on each, which `transactions()` does for every database
in the registry:

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

A test now writes to either one through its model, and both roll back when it
ends:

```python
@pytest.mark.db
def test_a_signup_is_recorded() -> None:
    register_user("ada@example.com")

    assert User.query.count() == 1
    assert Event.query.count() == 1  # `Event.__db__` is "warehouse"
```

### What a rollback on every database does not do

Each database rolls back its own transaction, on its own connection. Two things
follow, and both bite the first time:

- A test that writes to one database and reads from another sees only what is in
  the second. There is no transaction spanning both, and no isolation level
  changes that.
- A replica alias is a second connection, and it cannot see the test's
  uncommitted rows even when it points at the very same database.

The second one breaks the suite on the day a router arrives in the project.
Reads go to `replica`, the test's rows are committed nowhere, and every read
comes back empty. Keep routers off in tests, the way a fresh registry has none:

```python
@pytest.fixture(autouse=True)
def _db_marker() -> Iterator[None]:
    with db.transactions(rollback=True):
        yield
    db.route()  # nothing routed, reads and writes meet on default
```

A model that named its database through `__db__` keeps it: `route()` clears the
policy, not the model's own choice. Check the policy itself by where a model
lands, rather than by what it reads:

```python
def test_reads_go_to_the_replica() -> None:
    db.route(reads_go_to_the_replica)

    assert User.db is db["replica"]
```

### Counting queries across both

The free `assert_queries` watches every database in the registry, so a block
that works with two is counted as one number. To watch one, name it by alias or
hand over the database itself:

```python
from sqlakit.testing import assert_queries

with assert_queries(2):
    User.query.count()
    Event.query.count()

with assert_queries(1, using="warehouse"):
    build_report()
```

`db.assert_queries` watches one database, and on the registry it covers them
all.

A recording says which database ran what, which is how a test proves nothing
reached the warehouse:

```python
with db.recording() as record:
    register_user("ada@example.com")

assert record.databases == ("default",)
```

## Migrations instead of `create_all`

An application with migrations should test what it will deploy: run them once
per session, and leave the rollback around each test as it is.

Fail to pass the test's connection to `Alembic` and it opens one of its own, so
the migration runs outside your transaction. The rollback no longer takes it
back, and the schema outlives the run. Teach `env.py` to accept a connection
from outside, or nothing else here is worth doing:

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

`connection` is named the same on both sides: it is the key the fixtures below
put into `config.attributes`, and the argument of `context.configure`.

Two fixtures follow, one reading `alembic.ini` and one handing over the
connection. `Alembic` resolves `script_location` from the working directory, so
build the config from the project root and stay there while it works. Otherwise
the tests pass when run from the root and fail from anywhere else.

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

`Alembic` is synchronous, so on an async database the migrations go through
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

`run_sync` hands the function the synchronous connection from underneath, the
one `Alembic` wants and `env.py` then reads.

## Counting queries

`assert_queries` is a [recorder](debugging.md) with an assertion around it. It
counts the same way and prints the same statements on failure. It sits on the
database, next to `recording()`:

```python
@pytest.mark.db
def test_the_list_page_costs_two_queries() -> None:
    with db.assert_queries(2):
        User.query.order_by("name").page(limit=10)
```

The checks work on their own and together: an exact number, an `at_most`
ceiling, and `duplicates=False` to forbid repeats, which is how an N+1 is
caught.

```python
with db.assert_queries(at_most=5):
    ...  # a ceiling, for when an exact number is brittle
with db.assert_queries(duplicates=False):
    ...  # an N+1 test, with no number
with db.assert_queries(3, duplicates=False):
    ...
```

`db.assert_queries` watches the database it was called on. For several at once,
or for the configured registry, the function of the same name in
`sqlakit.testing` does it:

```python
from sqlakit.testing import assert_queries

with assert_queries(2):  # every database `sqlakit.db` has
    ...
with assert_queries(2, using="warehouse"):  # one of them, by alias
    ...
```

On failure everything that ran is printed, with the repeats pointing at one
another:

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

What it wants in place:

- **A database to watch.** `db.assert_queries` takes the one it was called on.
  The free function takes the whole registry, unless `using` named a single
  database.
- **A test that has a database at all**, meaning the `db` marker above it.
  Without one nothing is connected, and the code under test says so rather than
  counting zero.

The block stays `with` in both cases, under `asyncio` as well, because recording
listens rather than runs. When you want the numbers themselves rather than an
assertion, take `db.recording()`, the recorder this is built on.

## Reading what the code changed

The code under test writes on the test's connection, so the rows are in the
database already. The instance the test holds, though, still carries the values
it was loaded with. Read the row again:

```python
@pytest.mark.db
def test_revoking_a_token(token: Token) -> None:
    revoke(token.id)

    token.refresh()

    assert token.is_revoked
```

## Seeding data

A fixture that writes is like any other: it runs inside the test's transaction
and goes away with it.

```python
@pytest.fixture
def team() -> Team:
    return Team(name="red").save()
```

## What the test's block changes

A rolled-back block is an ordinary transaction, so the code under test behaves
the way it does in production. And it is already inside one:

- A nested `transaction()` joins the test's transaction on the same connection
  rather than opening its own. Its commit flushes, and what survives is up to
  the test's block alone.
- `autocommit()` joins as well and commits nothing inside the block, since it
  takes the connection the test bound, and everything written rolls back with
  the rest. It takes an `AUTOCOMMIT` connection of its own only when nothing is
  bound.
- A nested `transaction(rollback=True)` takes a savepoint, so it undoes its own
  writes and leaves the test's alone.
- A session that rolls *itself* back, through `session_factory()` and then
  `rollback()`, ends the whole transaction. The block then raises
  `TransactionRolledBackError`, having nothing left to commit. That is how
  production behaves rather than a test rig, and a block meant to fail on its
  own wants `transaction(savepoint=True)`.

The point is for the tests to behave the way production does. A rig that
forgives what production will not is only postponing the failure until release.

Next: [debugging](debugging.md) for watching the same queries outside a test.
