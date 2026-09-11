# Test setups

The [testing](testing.md) page is the suite the plugin runs for a project with
one database and its models on the model layer. This page is the rest: where
the schema is built, what a project on several databases writes, and the
fixtures to write when the plugin runs none of it.

## Schema creation

`sqlakit_schema` is the `provisioned_tables()` call. A suite with a schema of
its own replaces the fixture:

```python
@pytest.fixture(scope="session")
def sqlakit_schema() -> Iterator[None]:
    with db.transaction() as conn:
        conn.execute(sa.text(SCHEMA_SQL))
    yield
    with db.transaction() as conn:
        conn.execute(sa.text("DROP TABLE users"))
```

Migrations go here, and so does waiting for a server the tests start.
[Migrations instead of `create_all`](#migrations-instead-of-create_all) writes
that fixture out, and [starting a server](#server-startup) brings one up
with `pytest-docker`. An `asyncio` project writes it as an async fixture, under
the same name.

`sqlakit_base` is a shortcut for the two things the plugin takes from it, the
database the models live on and the tables to create. A project that creates
its own schema names the database instead, and the fixture creates nothing:

```python
@pytest.fixture(scope="session")
def sqlakit_db() -> Database:
    return db
```

## Projects without a model layer

A project on plain mapped classes, `SQLModel` among them, has no base to hand
over. Name the database and the metadata instead:

```python title="conftest.py"
import pytest
import sqlalchemy as sa

from sqlakit import Database

from app.db import db
from app.models import Base


@pytest.fixture(scope="session")
def sqlakit_db() -> Database:
    return db


@pytest.fixture(scope="session")
def sqlakit_metadata() -> sa.MetaData:
    return Base.metadata
```

The plugin creates the tables with `db.provisioned_tables(Base.metadata)`, the
same method the model layer uses.

The marker and the rollback work the same, and the tests read through the
database rather than through a model:

```python
@pytest.mark.db
def test_a_user_is_written() -> None:
    db.session.add(User(name="ada"))
    db.session.flush()

    assert db.query(User).count() == 1
```

## A base for each database

`sqlakit_base` names one base, so a project with a base per database names the
registry and builds both schemas:

```python title="conftest.py"
import pytest

from sqlakit import Databases

from app.db import db
from app.models import Model, WarehouseModel


@pytest.fixture(scope="session")
def sqlakit_db() -> Databases:
    return db


@pytest.fixture(scope="session")
def sqlakit_schema(sqlakit_db: Databases) -> Iterator[None]:
    with Model.provisioned_tables(), WarehouseModel.provisioned_tables():
        yield
```

The bases share the registry, and each says where its models live: `Model`
takes the default and `WarehouseModel` carries `__db__ = "warehouse"`. The
marker opens every database of the registry, and `using` still narrows it to
one.

## Databases with no registry between them

A project that builds its databases itself, and pins its models to them with
`set_db()`, names them in the fixture:

```python title="conftest.py"
from collections.abc import Iterator

import pytest

from sqlakit import Database

from app.db import orders_db, reporting_db
from app.models import OrderModel, ReportModel


@pytest.fixture(scope="session")
def sqlakit_db() -> dict[str, Database]:
    return {"orders": orders_db, "reporting": reporting_db}


@pytest.fixture(scope="session")
def sqlakit_schema(sqlakit_db: dict[str, Database]) -> Iterator[None]:
    with OrderModel.provisioned_tables(), ReportModel.provisioned_tables():
        yield
```

The marker opens a transaction on each and rolls them all back, and `using`
picks one by its key:

```python
@pytest.mark.db(using="reporting")
def test_the_quarterly_report() -> None: ...
```

A list works as well, `[orders_db, reporting_db]`. There `using` picks by the
name each database carries, so build them as
`Database(url, alias="reporting")`. Two unnamed databases are both `default`,
and a marker cannot pick between them: the plugin says so rather than guessing.

The keys of a dict name the databases for the marker alone. A recorded
statement carries the name its database was built with, whatever the fixture
calls it, so name a database you want to tell apart in a report.

The tables of each database are the project's to create, as above. A dict or a
list with `sqlakit_metadata` instead creates the same tables on every database,
as a suite over shards wants.

## Rollback limits

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
@pytest.fixture
def _db_transaction(_db_schema: None) -> Iterator[None]:
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

## Tables missing from the schema

`provisioned_tables()` creates the tables the metadata holds. A model whose
module was never imported is not in the metadata, so its tables are missing,
and the failure looks like a bug in the test. If your application keeps models
next to the features they belong to, import them all first:

```python
from sqlakit import import_models


@pytest.fixture(scope="session")
def _db_schema() -> Iterator[None]:
    import_models("app")
    with Model.provisioned_tables():
        yield
```

## Server startup

Migrations are usually written for the database you deploy on, not for
`SQLite`. `pytest-docker` starts one for the session, and the schema fixture
waits for it before running anything:

```yaml title="tests/docker-compose.yaml"
services:
  postgres:
    image: postgres:18
    environment:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: app
      POSTGRES_DB: app_test
    ports:
      - "7432:5432"
```

```python
import contextlib
import pathlib
from collections.abc import Iterator

import pytest

from sqlakit import Database

URL = "postgresql+psycopg://app:app@127.0.0.1:7432/app_test"


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig: pytest.Config) -> Iterator[pathlib.Path]:
    with contextlib.chdir(pytestconfig.rootpath):
        yield pytestconfig.rootpath / "tests" / "docker-compose.yaml"


def _db_is_up() -> bool:
    with Database(URL) as probe:
        return probe.ping()


@pytest.fixture(scope="session")
def postgres(docker_services: pytest.FixtureRequest) -> str:
    """Return the URL of a server that answers."""
    docker_services.wait_until_responsive(timeout=60.0, pause=1.0, check=_db_is_up)
    return URL
```

`docker compose` reads the paths inside the file against the working
directory, so the block holds the tests at the project root however they were
started. The container takes a while to accept connections, and
`wait_until_responsive` holds the first test back until it does. Ask for the
fixture where the schema is created:

```python
@pytest.fixture(scope="session")
def sqlakit_schema(
    postgres: str, alembic_config: alembic.config.Config
) -> Iterator[None]: ...
```

Point the application at the same URL, in `conftest.py` or in the settings the
tests load, or the migrations and the tests will run on two different servers.

## Migrations instead of `create_all`

If your application has migrations, test the schema you'll actually deploy.
`sqlakit_schema` is where they run, once per session, and the rollback around
each test stays as it is.

If you don't pass the test's connection to `Alembic`, it opens one of its own
and the migration runs outside your transaction. The rollback can't undo it,
and the schema outlives the run. So your `env.py` needs to accept a connection
from outside. The rest of this section depends on it:

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
def sqlakit_schema(alembic_config: alembic.config.Config) -> Iterator[None]:
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
async def sqlakit_schema(
    alembic_config: alembic.config.Config,
) -> AsyncIterator[None]:
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

## Tests without the plugin

The same marker, written by hand:

```python title="conftest.py (by hand)"
from collections.abc import Iterator

import pytest

from app.db import db
from app.models import Model


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: the test needs a database")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give a database to the marked tests and to nothing else."""
    for item in items:
        if isinstance(item, pytest.Function) and item.get_closest_marker("db"):
            item.fixturenames.insert(0, "_db_transaction")


@pytest.fixture(scope="session")
def _db_schema() -> Iterator[None]:
    with Model.provisioned_tables():
        yield


@pytest.fixture
def _db_transaction(_db_schema: None) -> Iterator[None]:
    with db.transaction(rollback=True):
        yield
```

An unmarked test never asks for `_db_transaction`, so nothing connects, and
`_db_schema` runs only when some test does ask.

### Several databases by hand

Pass the alias, and each database gets the tables of the models that point at
it. An association table lands on the same database as the rows it joins:

```python
@pytest.fixture(scope="session")
def _db_schema() -> Iterator[None]:
    with Model.provisioned_tables(), Model.provisioned_tables("warehouse"):
        yield
```

Then open a transaction on each one. `transactions()` does that for every
database in the registry:

```python
@pytest.fixture
def _db_transaction(_db_schema: None) -> Iterator[None]:
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

### Async tests without the plugin

The fixtures become async, because the transaction has to open on the loop the
test runs on. The marker and the hook are the same:

```python title="conftest.py (by hand, asyncio)"
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
            item.fixturenames.insert(0, "_db_transaction")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
async def _db_schema() -> AsyncIterator[None]:
    async with Model.provisioned_tables():
        yield


@pytest.fixture
async def _db_transaction(_db_schema: None) -> AsyncIterator[None]:
    async with db.transaction(rollback=True):
        yield
```

`pytest` reads `item.fixturenames` at collection time, and the fixtures set up
in that order. `insert(0, ...)` puts the transaction before the ones the test
asked for, so a fixture that writes rows writes them inside it. Appending
leaves those rows outside the rollback.

A fixture of a wider scope, one that seeds a whole module, needs the
transaction after it instead. The plugin works that position out for you.

Mark your tests as `anyio`, or make the `anyio_backend` fixture above
`autouse=True` and mark none of them:

```python
@pytest.mark.anyio
@pytest.mark.db
async def test_renaming_a_user() -> None:
    user = await User(name="ada").save()

    await rename(user.id, "grace")

    await user.refresh()

    assert user.name == "grace"
```

Next: [testing](testing.md) for the marker itself, or
[debugging](debugging.md) for the same recorder outside a test.
