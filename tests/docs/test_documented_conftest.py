"""The conftest the testing page shows is run here, so it cannot go stale."""

import re
from pathlib import Path

import pytest

DOCS = Path(__file__).parent.parent.parent / "docs" / "testing.md"

APP_DB = """
from sqlakit import Database

db = Database("sqlite:///app.db")
"""

ASYNC_APP_DB = """
from sqlakit.asyncio import Database

db = Database("sqlite+aiosqlite:///app.db")
"""

# A base of its own, as the docs advise: the shipped `Model` carries one
# metadata for the whole process, and this project shares that process.
MODELS = """
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db import db
from {orm} import ModelMixin


class Model(ModelMixin, DeclarativeBase):
    __db__ = db


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
"""


DISPOSE = """

@pytest.fixture(scope="session", autouse=True)
async def _dispose() -> AsyncIterator[None]:
    yield
    await db.dispose()
"""


def _block(title: str) -> str:
    """Return the code of the block the page gives that title."""
    pattern = rf'```python title="{re.escape(title)}"\n(.*?)```'
    found = re.search(pattern, DOCS.read_text(), re.DOTALL)
    assert found is not None, f"no block titled {title!r} in {DOCS}"
    return found.group(1)


def _project(pytester: pytest.Pytester, *, asyncio: bool) -> None:
    """Write the application the documented conftest imports."""
    app = pytester.path / "app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "db.py").write_text(ASYNC_APP_DB if asyncio else APP_DB)
    (app / "models.py").write_text(
        MODELS.format(orm="sqlakit.asyncio.orm" if asyncio else "sqlakit.orm")
    )


def test_the_documented_conftest_isolates_each_test(pytester: pytest.Pytester) -> None:
    _project(pytester, asyncio=False)
    pytester.makeconftest(_block("conftest.py (by hand)"))
    pytester.makepyfile(
        test_users="""
        import pytest

        from app.models import User


        @pytest.mark.db
        def test_writes_are_visible_to_the_test() -> None:
            user = User(name="ada").save()
            user.refresh()

            assert user.name == "ada"


        @pytest.mark.db
        def test_the_next_test_starts_clean() -> None:
            assert User.query.count() == 0


        def test_a_test_without_the_marker_needs_no_database() -> None:
            assert True
        """
    )

    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=3)


def test_the_documented_async_conftest_isolates_each_test(
    pytester: pytest.Pytester,
) -> None:
    _project(pytester, asyncio=True)
    # The documented conftest, and a teardown of this harness's own: the
    # sub-session runs in this process, and an `aiosqlite` engine holds a
    # thread until something disposes of it.
    pytester.makeconftest(_block("conftest.py (by hand, asyncio)") + DISPOSE)
    pytester.makepyfile(
        test_users="""
        import pytest

        from app.models import User


        @pytest.mark.anyio
        @pytest.mark.db
        async def test_writes_are_visible_to_the_test() -> None:
            user = await User(name="ada").save()
            await user.refresh()

            assert user.name == "ada"


        @pytest.mark.anyio
        @pytest.mark.db
        async def test_the_next_test_starts_clean() -> None:
            assert await User.query.count() == 0
        """
    )

    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=2)


def test_a_session_without_a_marked_test_builds_no_schema(
    pytester: pytest.Pytester,
) -> None:
    _project(pytester, asyncio=False)
    pytester.makeconftest(_block("conftest.py (by hand)"))
    pytester.makepyfile(
        test_plain="""
        def test_needs_no_database() -> None:
            assert True
        """
    )

    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=1)

    assert not (pytester.path / "app.db").exists()
