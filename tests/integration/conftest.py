"""The databases the tests here need, in containers.

One compose file for all of them: `pytest-docker` reads it once a session, so
a second one would take the first one's place. Everything is skipped when
Docker is not installed, which keeps the rest of the suite runnable anywhere.
"""

import contextlib
import pathlib
import shutil
from collections.abc import AsyncIterator, Iterator

import pytest
import sqlalchemy as sa
import sqlalchemy.exc

from sqlakit import Database
from sqlakit.asyncio import Database as AsyncDatabase

ORACLE = "oracle+oracledb://sqlakit:sqlakit@127.0.0.1:7521/?service_name=FREEPDB1"

URLS = {
    "postgres": "postgresql+psycopg://sqlakit:sqlakit@127.0.0.1:7433/sqlakit_test",
    "mysql": "mysql+pymysql://sqlakit:sqlakit@127.0.0.1:7306/sqlakit_test",
    "mariadb": "mysql+pymysql://sqlakit:sqlakit@127.0.0.1:7307/sqlakit_test",
    "oracle": ORACLE,
}
"""Every database the suite runs against, by the name a test parametrises on."""

ASYNC_URLS = {
    "postgres": URLS["postgres"],
    "mysql": "mysql+aiomysql://sqlakit:sqlakit@127.0.0.1:7306/sqlakit_test",
    "mariadb": "mysql+aiomysql://sqlakit:sqlakit@127.0.0.1:7307/sqlakit_test",
    "oracle": ORACLE.replace("oracle+oracledb", "oracle+oracledb_async"),
}

WAIT = {"oracle": 300.0}
"""How long a database may take to come up, when the default is not enough."""


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def docker_compose_file(pytestconfig: pytest.Config) -> Iterator[list[pathlib.Path]]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    here = pathlib.Path(__file__).parent
    with contextlib.chdir(pytestconfig.rootpath):
        yield [here / "docker-compose.yaml"]


def _responds(url: str) -> bool:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(
                sa.text("SELECT 1 FROM dual" if "oracle" in url else "SELECT 1")
            )
    except sa.exc.DBAPIError:
        return False
    else:
        return True
    finally:
        engine.dispose()


@pytest.fixture(scope="session", params=sorted(URLS))
def dialect(request: pytest.FixtureRequest) -> str:
    """The name of the database a test runs against."""
    return str(request.param)


@pytest.fixture(scope="session")
def url(dialect: str, docker_services: pytest.FixtureRequest) -> str:
    """Return the URL of a database that answers."""
    address = URLS[dialect]
    docker_services.wait_until_responsive(  # ty: ignore[unresolved-attribute]
        timeout=WAIT.get(dialect, 120.0), pause=1.0, check=lambda: _responds(address)
    )
    return address


@pytest.fixture
def db(url: str) -> Iterator[Database]:
    db = Database(url)
    yield db
    db.dispose()


@pytest.fixture
async def async_db(dialect: str, url: str) -> AsyncIterator[AsyncDatabase]:
    db = AsyncDatabase(ASYNC_URLS[dialect])
    yield db
    await db.dispose()


@pytest.fixture(scope="session")
def postgres(docker_services: pytest.FixtureRequest) -> str:
    """The PostgreSQL URL, for the tests that are about PostgreSQL alone."""
    address = URLS["postgres"]
    docker_services.wait_until_responsive(  # ty: ignore[unresolved-attribute]
        timeout=120.0, pause=0.5, check=lambda: _responds(address)
    )
    return address


@pytest.fixture
def postgres_db(postgres: str) -> Iterator[Database]:
    db = Database(postgres)
    yield db
    db.dispose()
