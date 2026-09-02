"""The `db` marker, and the fixtures behind it.

Installed with the library, so a project writes the two lines that say which
database and which tables, and nothing else:

```python title="conftest.py"
import pytest

from app.db import db
from app.models import Model


@pytest.fixture(scope="session")
def sqlakit_db() -> Databases:
    return db


@pytest.fixture(scope="session")
def sqlakit_metadata() -> sa.MetaData:
    return Model.metadata
```

A test marked `db` runs in a transaction that rolls back, on every database.
`using` narrows that to the ones a test works on:

```python
@pytest.mark.db(using="warehouse")
@pytest.mark.db(using=["default", "warehouse"])
```

Every other test connects to nothing, and reaching for a session there raises
`MissingSessionError` rather than opening one.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
import time
import warnings
from contextlib import AsyncExitStack, ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

import pytest

from ._debugserver import as_payload, write_report
from ._registry import db as importable_db

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import sqlalchemy as sa

MARKER = "db"
SYNC_FIXTURE = "_sqlakit_transaction"
ASYNC_FIXTURE = "_sqlakit_async_transaction"
REPORT = pytest.StashKey[list[dict[str, Any]]]()
WHERE = pytest.StashKey["pathlib.Path | None"]()
NAMED_BY_THE_CLOCK = ""
"""What `--sqlakit-report` with no path means."""


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(
        "sqlakit",
        "give the tests marked `db` a database, and the rest none",
        type="bool",
        default=False,
    )
    parser.addini(
        "sqlakit_skip_queries_from",
        "the queries these files run stay out of the report: a factory, a helper",
        type="paths",
        default=[],
    )
    parser.addoption(
        "--sqlakit-report",
        metavar="PATH",
        nargs="?",
        const=NAMED_BY_THE_CLOCK,
        default=None,
        help=(
            "write a page of every statement the marked tests ran; "
            "on its own the name carries the time the run finished"
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getini("sqlakit"):
        config.addinivalue_line("markers", f"{MARKER}: the test needs a database")
    config.stash[REPORT] = []
    config.stash[WHERE] = _report_path(config)  # named now, so a bad path fails now


def pytest_terminal_summary(
    terminalreporter: Any,  # noqa: ANN401 - the reporter of whichever pytest runs
    config: pytest.Config,
) -> None:
    """Write the report at the end, and say where it went."""
    where = config.stash.get(WHERE, None)
    records = config.stash.get(REPORT, [])
    if where is None or not records:
        return
    tests = len({record["app"] + record["label"] for record in records})
    queries = sum(record["count"] for record in records)
    written = write_report(where, records, about=f"{tests} tests · {queries} queries")
    terminalreporter.write_line(f"sqlakit wrote {written}")


def _report_path(config: pytest.Config) -> pathlib.Path | None:
    """Return where the report goes, if one was asked for.

    ``--sqlakit-report`` on its own names the file after the clock, so a run
    keeps the one before it.
    """
    asked = config.getoption("--sqlakit-report", default=None)
    if asked is None:
        return None
    if asked == NAMED_BY_THE_CLOCK:
        return config.rootpath / time.strftime("sqlakit-%Y%m%d-%H%M%S.html")
    where = pathlib.Path(asked)
    if where.is_dir():
        # `--sqlakit-report tests/unit` reads as the path to write to.
        message = (
            f"--sqlakit-report was given `{asked}`, which is a directory. "
            f"Write `--sqlakit-report=PATH` for a file, or the flag on its own "
            f"for a name with the time in it."
        )
        raise pytest.UsageError(message)
    return where


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Give a database to the marked tests and to nothing else.

    The fixture is added here rather than with `usefixtures` because an async
    one cannot be fetched from a synchronous fixture, and `pytest` reads
    `fixturenames` at this moment.
    """
    if not config.getini("sqlakit"):
        return
    for item in items:
        if not isinstance(item, pytest.Function):
            continue
        if item.get_closest_marker(MARKER) is None:
            continue
        awaited = inspect.iscoroutinefunction(item.function)
        name = ASYNC_FIXTURE if awaited else SYNC_FIXTURE
        item.fixturenames.insert(_before_the_test_s_own(item), name)


def _before_the_test_s_own(item: pytest.Function) -> int:
    """Return where the transaction goes among the fixtures of a test.

    `fixturenames` is the order they set up in. A fixture of the test's own
    that writes has to run inside the transaction, and one of a wider scope,
    the schema or a module that seeds itself, has to run outside it.
    """
    known = item._fixtureinfo.name2fixturedefs  # noqa: SLF001
    for index, name in enumerate(item.fixturenames):
        defined = known.get(name)
        if defined and defined[-1].scope == "function":
            return index
    return len(item.fixturenames)


@pytest.fixture(scope="session")
def sqlakit_db(sqlakit_base: Any) -> Any:  # noqa: ANN401
    """Return the database the marked tests run on.

    The one the models live on: their registry, which knows every alias, or the
    `Database` they were given in person. Override it for a project with no
    model layer.
    """
    if sqlakit_base is None:
        if not importable_db.is_configured:
            pytest.fail(
                "sqlakit has no database to test on. Define `sqlakit_base` with "
                "the base your models inherit, or `sqlakit_db` with the database "
                "itself.",
                pytrace=False,
            )
        return importable_db
    registry = getattr(sqlakit_base, "dbs", None)
    if (
        isinstance(getattr(sqlakit_base, "__db__", None), str)
        and registry is not None
        and registry.is_configured
    ):
        # The models look their aliases up there, so the schema and the
        # transactions cover every one of them, not the default alone.
        return registry
    database = getattr(sqlakit_base, "db", None)
    if database is None:
        pytest.fail(
            f"`sqlakit_base` returned `{sqlakit_base.__name__}`, which is not on "
            "the model layer and names no database. Return the base your models "
            "inherit, or define `sqlakit_db` instead.",
            pytrace=False,
        )
    return database


@pytest.fixture(scope="session")
def sqlakit_base() -> Any | None:  # noqa: ANN401
    """Return the declarative base of the models under test, or None.

    Every alias gets the tables of the models pointed at it, so a project on
    more than one database says this and nothing else.
    """
    return None


@pytest.fixture(scope="session")
def sqlakit_metadata() -> sa.MetaData | None:
    """Return the tables to create for the session, or None to create none.

    For metadata outside the model layer. None suits a suite running against a
    schema built elsewhere, by migrations or by a fixture of the project's own.
    """
    return None


@pytest.fixture(scope="session")
def sqlakit_schema(
    sqlakit_db: Any,  # noqa: ANN401
    sqlakit_base: Any,  # noqa: ANN401
    sqlakit_metadata: sa.MetaData | None,
) -> Iterator[None]:
    """Build the schema for the session, and take it down after.

    Override it to build the schema another way, `Alembic` against a server
    `pytest-docker` started among them:

    ```python
    @pytest.fixture(scope="session")
    def sqlakit_schema(alembic_config, _postgres):
        alembic.command.upgrade(alembic_config, "head")

    Yield:
        alembic.command.downgrade(alembic_config, "base")
    ```

    """
    with _entered(_schema_blocks(sqlakit_db, sqlakit_base, sqlakit_metadata)):
        yield


@pytest.fixture(scope="session")
def sqlakit_seed(sqlakit_schema: None) -> None:  # noqa: ARG001 - after the schema
    """Write the rows every test starts from, committed, once for the session.

    A test's own writes roll back around it, and these stay:

    ```python
    @pytest.fixture(scope="session")
    def sqlakit_seed(sqlakit_schema):
        with db.transaction():
            Plan(name="free").save()
    ```

    It runs when the first marked test does, so a suite that needs no database
    writes nothing.
    """
    return


@pytest.fixture
def _sqlakit_transaction(
    request: pytest.FixtureRequest,
    sqlakit_db: Any,  # noqa: ANN401
    sqlakit_seed: None,  # noqa: ARG001 - requested so the rows are there
) -> Iterator[None]:
    with ExitStack() as stack:
        for block in _rolled_back(sqlakit_db, _asked_for(request)):
            stack.enter_context(block)
        with _reported(request, sqlakit_db):
            yield


@pytest.fixture
async def _sqlakit_async_transaction(
    request: pytest.FixtureRequest,
    sqlakit_db: Any,  # noqa: ANN401
    sqlakit_seed: None,  # noqa: ARG001 - requested so the rows are there
) -> AsyncIterator[None]:
    async with AsyncExitStack() as stack:
        for block in _rolled_back(sqlakit_db, _asked_for(request)):
            await stack.enter_async_context(block)
        with _reported(request, sqlakit_db):
            yield


@contextmanager
def _reported(request: pytest.FixtureRequest, db: Any) -> Iterator[None]:  # noqa: ANN401
    """Record what the test runs, for the report, when one was asked for.

    The recording is named after the test and filed under the file it lives in,
    so the page groups a run the way the suite is laid out.
    """
    if request.config.stash.get(WHERE, None) is None:
        yield
        return
    node = request.node
    skip = request.config.getini("sqlakit_skip_queries_from")
    with db.recording(_named(node), stacks=True, skip_queries_from=skip) as recording:
        yield
    request.config.stash[REPORT].append(
        as_payload(
            recording,
            app=str(node.path.relative_to(request.config.rootpath)),
            tags=[mark.name for mark in node.iter_markers() if mark.name != MARKER],
        )
    )


def _named(node: pytest.Item) -> str:
    """Return the test as the suite names it, the class it is in included."""
    return "::".join(node.nodeid.split("::")[1:]) or node.name


@contextmanager
def _entered(blocks: list[Any]) -> Iterator[None]:
    """Hold these blocks open, awaiting the ones that need it.

    An `asyncio` database builds its schema in a coroutine, and this fixture is
    not one: a session of its own runs it, which the tests never touch. They
    open their transactions on the loop `anyio` gives them.
    """
    if not any(hasattr(block, "__aenter__") for block in blocks):
        with ExitStack() as stack:
            for block in blocks:
                stack.enter_context(block)
            yield
        return

    stack = AsyncExitStack()
    loop = asyncio.new_event_loop()
    try:
        for block in blocks:
            loop.run_until_complete(stack.enter_async_context(block))
        yield
    finally:
        loop.run_until_complete(stack.aclose())
        loop.close()


def _schema_blocks(
    db: Any,  # noqa: ANN401
    base: Any,  # noqa: ANN401
    metadata: sa.MetaData | None,
) -> list[Any]:
    """Return the blocks that create the schema, one per database.

    The model layer knows which tables belong on which alias, so a registry
    gets one block per alias. Without it there is one metadata, and one
    database to put it on.
    """
    if base is not None:
        aliases = getattr(db, "aliases", None) or (None,)
        return [base.provisioned_tables(alias) for alias in aliases]
    if metadata is not None:
        return [db.provisioned_tables(metadata)]
    warnings.warn(
        "sqlakit creates no tables: neither `sqlakit_base` nor "
        "`sqlakit_metadata` is defined. Define one, or replace "
        "`sqlakit_schema` with the fixture that builds your schema.",
        stacklevel=2,
    )
    return []


def _asked_for(request: pytest.FixtureRequest) -> tuple[Any, ...]:
    """Return the databases the marker asks for, by name or in person."""
    marker = request.node.get_closest_marker(MARKER)
    using = None if marker is None else marker.kwargs.get("using")
    if using is None:
        return ()
    if isinstance(using, str) or not isinstance(using, (list, tuple, set, frozenset)):
        return (using,)
    return tuple(using)


def _rolled_back(db: Any, using: tuple[Any, ...]) -> list[Any]:  # noqa: ANN401
    """Return the blocks that undo what a test writes.

    A marker with no ``using`` opens every database, which for most projects is
    the one they have. Naming one is how a project on several stops paying for
    a connection to each in the tests that read one.
    """
    if not using:
        return [
            db.transactions(rollback=True)
            if len(getattr(db, "aliases", ()) or ()) > 1
            else db.transaction(rollback=True)
        ]
    return [
        (db[one] if isinstance(one, str) else one).transaction(rollback=True)
        for one in using
    ]
