"""The plugin, run over a project of its own.

The project lives in `tests/projects/plugin`, as files rather than as strings
here, so the linters read it too.
"""

from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent / "projects" / "plugin"
REGISTERED = Path(__file__).parent.parent / "projects" / "registered"
CONFTEST = (PROJECT / "conftest.py").read_text()


def _copied(pytester: pytest.Pytester, project: Path) -> pytest.Pytester:
    for name in ("app.py", "conftest.py"):
        (pytester.path / name).write_text((project / name).read_text())
    (pytester.path / "pytest.ini").write_text("[pytest]\nsqlakit = true\n")
    return pytester


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    return _copied(pytester, PROJECT)


@pytest.fixture
def registered(pytester: pytest.Pytester) -> pytest.Pytester:
    """A project whose databases were registered rather than configured."""
    return _copied(pytester, REGISTERED)


def test_the_marker_alone_opens_every_database(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_one="""
        import pytest

        from app import Event, User


        @pytest.mark.db
        def test_it():
            User(name="ada").save()
            Event(what="signup").save()
            assert (User.query.count(), Event.query.count()) == (1, 1)
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_databases_registered_rather_than_configured(
    registered: pytest.Pytester,
) -> None:
    """Every alias gets its tables and its transaction, the registered default too."""
    registered.makepyfile(
        test_registered="""
        import pytest

        from app import Event, Model, User


        @pytest.mark.db
        def test_writes_to_both():
            User(name="ada").save()
            Event(what="signup").save()
            assert (User.query.count(), Event.query.count()) == (1, 1)


        @pytest.mark.db
        def test_both_rolled_back():
            assert (User.query.count(), Event.query.count()) == (0, 0)


        @pytest.mark.db(using="warehouse")
        def test_one_of_them_by_name():
            Event(what="signup").save()
            assert Event.query.count() == 1
        """
    )

    registered.runpytest_subprocess().assert_outcomes(passed=3)


def test_the_marker_opens_the_databases_using_names(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_two="""
        import pytest

        from app import Event, User


        @pytest.mark.db(using=["default", "warehouse"])
        def test_writes_both():
            User(name="ada").save()
            Event(what="signup").save()
            assert (User.query.count(), Event.query.count()) == (1, 1)


        @pytest.mark.db(using=["default", "warehouse"])
        def test_both_rolled_back():
            assert (User.query.count(), Event.query.count()) == (0, 0)
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=2)


def test_an_unmarked_test_reaches_no_database(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_three="""
        import pytest

        from app import User
        from sqlakit import MissingSessionError


        def test_it():
            with pytest.raises(MissingSessionError):
                User.query.count()
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_the_plugin_stays_out_of_the_way_until_it_is_asked(
    project: pytest.Pytester,
) -> None:
    (project.path / "pytest.ini").write_text("[pytest]\n")
    project.makepyfile(
        test_four="""
        import pytest

        from app import User


        @pytest.mark.db
        def test_it():
            User(name="ada").save()
        """
    )

    result = project.runpytest_subprocess()

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*MissingSessionError*"])


def test_a_project_may_build_the_schema_its_own_way(project: pytest.Pytester) -> None:
    """A suite running migrations against a server of its own does this."""
    (project.path / "conftest.py").write_text(
        CONFTEST
        + """

@pytest.fixture(scope="session")
def sqlakit_schema():
    from app import Model, db

    with db.transaction() as conn:
        Model.metadata.create_all(conn)  # a migration would run here
    yield
    with db.transaction() as conn:
        Model.metadata.drop_all(conn)
"""
    )
    project.makepyfile(
        test_five="""
        import pytest

        from app import User


        @pytest.mark.db
        def test_it():
            User(name="ada").save()
            assert User.query.count() == 1
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_using_takes_a_database_as_well_as_a_name(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_six="""
        import pytest

        from app import Event, db


        @pytest.mark.db(using=db["warehouse"])
        def test_it():
            Event(what="signup").save()
            assert Event.query.count() == 1
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_using_names_one_database(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_seven="""
        import pytest

        from app import Event, User
        from sqlakit import MissingSessionError


        @pytest.mark.db(using="warehouse")
        def test_it():
            Event(what="signup").save()
            assert Event.query.count() == 1
            with pytest.raises(MissingSessionError):
                User.query.count()
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_seed_rows_outlive_a_test_and_its_rollback(project: pytest.Pytester) -> None:
    (project.path / "conftest.py").write_text(
        CONFTEST
        + """

@pytest.fixture(scope="session")
def sqlakit_seed(sqlakit_schema):
    from app import User, db

    with db.transaction():
        for name in ("ada", "grace"):
            User(name=name).save()
"""
    )
    project.makepyfile(
        test_eight="""
        import pytest

        from app import User


        @pytest.mark.db
        def test_writes_one_of_its_own():
            assert User.query.count() == 2
            User(name="mine").save()
            assert User.query.count() == 3


        @pytest.mark.db
        def test_only_the_seeds_are_left():
            assert [user.name for user in User.query.order_by("id").all()] == [
                "ada",
                "grace",
            ]
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=2)


def test_the_seed_waits_for_a_test_that_needs_it(project: pytest.Pytester) -> None:
    (project.path / "conftest.py").write_text(
        CONFTEST
        + """

@pytest.fixture(scope="session")
def sqlakit_seed(sqlakit_schema):
    raise AssertionError("nothing asked for a database")
"""
    )
    project.makepyfile(
        test_nine="""
        def test_it():
            assert True
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_the_marker_works_on_a_module_and_on_a_class(
    project: pytest.Pytester,
) -> None:
    project.makepyfile(
        test_ten="""
        import pytest

        from app import Event, User
        from sqlakit import MissingSessionError

        pytestmark = pytest.mark.db


        def test_the_module_marker_gives_the_default():
            User(name="ada").save()
            assert User.query.count() == 1


        @pytest.mark.db(using="warehouse")
        class TestTheWarehouse:
            def test_the_class_marker_wins(self):
                Event(what="signup").save()
                assert Event.query.count() == 1

            @pytest.mark.db(using=["default", "warehouse"])
            def test_the_method_marker_wins_over_the_class(self):
                User(name="grace").save()
                Event(what="signup").save()
                assert (User.query.count(), Event.query.count()) == (1, 1)
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=3)


def test_a_module_may_seed_rows_of_its_own(project: pytest.Pytester) -> None:
    """A transaction around the module, with each test nested in it."""
    project.makepyfile(
        test_eleven="""
        import pytest

        from app import User, db

        pytestmark = pytest.mark.db


        @pytest.fixture(scope="module", autouse=True)
        def _seeded(sqlakit_schema):  # the schema is built before the rows go in
            with db.transaction(rollback=True):  # undone when the module ends
                User(name="ada").save()
                yield


        def test_sees_the_module_rows():
            assert User.query.count() == 1
            User(name="mine").save()
            assert User.query.count() == 2


        def test_keeps_them_and_loses_the_other_test_s_row():
            assert [user.name for user in User.query.all()] == ["ada"]
        """,
        test_twelve="""
        import pytest

        from app import User

        pytestmark = pytest.mark.db


        def test_another_module_sees_nothing():
            assert User.query.count() == 0
        """,
    )

    project.runpytest_subprocess().assert_outcomes(passed=3)


def test_an_async_project_overrides_the_same_names(project: pytest.Pytester) -> None:
    """The schema is built off the tests' loop, so one name serves both."""
    (project.path / "app.py").write_text(
        """
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import ModelMixin

db = Database("sqlite+aiosqlite:///./async.db")


class Model(ModelMixin, DeclarativeBase):
    __db__ = db


class Plan(Model):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
"""
    )
    (project.path / "conftest.py").write_text(
        """
import pytest

from app import Model, Plan, db


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def sqlakit_base():
    return Model


@pytest.fixture(scope="session")
async def sqlakit_seed(sqlakit_schema):
    async with db.transaction():
        await Plan(name="free").save()
"""
    )
    project.makepyfile(
        test_async="""
        import pytest

        from app import Plan


        @pytest.mark.anyio
        @pytest.mark.db
        async def test_the_seed_is_there():
            assert [p.name for p in await Plan.query.all()] == ["free"]
            await Plan(name="mine").save()


        @pytest.mark.anyio
        @pytest.mark.db
        async def test_only_the_seed_is_left():
            assert [p.name for p in await Plan.query.all()] == ["free"]
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=2)


def test_an_autouse_backend_runs_async_tests_unmarked(
    project: pytest.Pytester,
) -> None:
    """`anyio` runs a coroutine when `anyio_backend` reaches it, marker or not."""
    (project.path / "app.py").write_text(
        """
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import ModelMixin

db = Database("sqlite+aiosqlite:///./autouse.db")


class Model(ModelMixin, DeclarativeBase):
    __db__ = db


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
"""
    )
    (project.path / "conftest.py").write_text(
        """
import pytest

from app import Model


@pytest.fixture(scope="session", autouse=True)
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def sqlakit_base():
    return Model
"""
    )
    project.makepyfile(
        test_unmarked="""
        import pytest

        from app import User


        @pytest.mark.db
        async def test_writes():
            await User(name="ada").save()
            assert await User.query.count() == 1


        @pytest.mark.db
        async def test_rolled_back():
            assert await User.query.count() == 0
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=2)


def test_a_project_may_name_the_database_instead_of_the_base(
    project: pytest.Pytester,
) -> None:
    """A suite with migrations of its own: no base, no metadata."""
    (project.path / "conftest.py").write_text(
        """
import pytest

from app import Model, db


@pytest.fixture(scope="session")
def sqlakit_db():
    return db


@pytest.fixture(scope="session")
def sqlakit_schema():
    with db["default"].transaction() as conn:
        Model.metadata.create_all(conn)
    yield
    with db["default"].transaction() as conn:
        Model.metadata.drop_all(conn)
"""
    )
    project.makepyfile(
        test_named="""
        import pytest

        from app import User


        @pytest.mark.db(using="default")
        def test_it():
            User(name="ada").save()
            assert User.query.count() == 1
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_it_says_when_it_was_told_no_tables(project: pytest.Pytester) -> None:
    (project.path / "conftest.py").write_text(
        """
import pytest

from app import db


@pytest.fixture(scope="session")
def sqlakit_db():
    return db
"""
    )
    project.makepyfile(
        test_silent="""
        import pytest


        @pytest.mark.db(using="default")
        def test_it():
            assert True
        """
    )

    result = project.runpytest_subprocess()

    result.stdout.fnmatch_lines(["*sqlakit creates no tables*"])


def test_it_says_when_no_database_was_named(project: pytest.Pytester) -> None:
    (project.path / "app.py").write_text(
        """
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database, EngineArgs
from sqlakit.orm import ModelMixin

ARGS: EngineArgs = {"poolclass": sa.StaticPool}
own = Database("sqlite://", engine_args=ARGS)


class Model(ModelMixin, DeclarativeBase):
    __db__ = own


class Plain(DeclarativeBase):
    pass


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
"""
    )
    (project.path / "conftest.py").write_text("")
    project.makepyfile(
        test_nothing="""
        import pytest


        @pytest.mark.db
        def test_it():
            assert True
        """
    )

    result = project.runpytest_subprocess()

    result.stdout.fnmatch_lines(["*sqlakit has no database to test on*"])


def test_it_says_when_the_base_is_not_one(project: pytest.Pytester) -> None:
    (project.path / "conftest.py").write_text(
        """
import pytest
from sqlalchemy.orm import DeclarativeBase


class Plain(DeclarativeBase):
    pass


@pytest.fixture(scope="session")
def sqlakit_base():
    return Plain
"""
    )
    project.makepyfile(
        test_plain="""
        import pytest


        @pytest.mark.db
        def test_it():
            assert True
        """
    )

    result = project.runpytest_subprocess()

    result.stdout.fnmatch_lines(["*is not on the model layer*"])


def test_metadata_serves_a_project_with_no_model_layer(
    project: pytest.Pytester,
) -> None:
    """`SQLModel`, or any plain mapped class, needs this."""
    (project.path / "app.py").write_text(
        """
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database, EngineArgs

ARGS: EngineArgs = {"poolclass": sa.StaticPool}
db = Database("sqlite://", engine_args=ARGS)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
"""
    )
    (project.path / "conftest.py").write_text(
        """
import pytest

from app import Base, db


@pytest.fixture(scope="session")
def sqlakit_db():
    return db


@pytest.fixture(scope="session")
def sqlakit_metadata():
    return Base.metadata
"""
    )
    project.makepyfile(
        test_metadata="""
        import pytest

        from app import User, db


        @pytest.mark.db
        def test_it():
            db.session.add(User(name="ada"))
            db.session.flush()
            assert db.query(User).count() == 1


        @pytest.mark.db
        def test_rolled_back():
            assert db.query(User).count() == 0
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=2)


def test_a_fixture_of_the_test_s_own_writes_inside_the_transaction(
    project: pytest.Pytester,
) -> None:
    """The transaction opens before the fixtures the test asked for."""
    project.makepyfile(
        test_order="""
        import pytest

        from app import User


        @pytest.fixture
        def a_user():
            User(name="from a fixture").save()


        @pytest.mark.db
        @pytest.mark.usefixtures("a_user")
        def test_the_row_is_visible():
            assert User.query.where(User.name == "from a fixture").one_or_none()


        @pytest.mark.db
        def test_the_row_rolled_back():
            assert User.query.where(User.name == "from a fixture").one_or_none() is None
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=2)


def test_the_order_holds_without_the_model_layer(project: pytest.Pytester) -> None:
    """A project on plain mapped classes writes through `db.session`."""
    (project.path / "app.py").write_text(
        """
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database, EngineArgs

ARGS: EngineArgs = {"poolclass": sa.StaticPool}
db = Database("sqlite://", engine_args=ARGS)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
"""
    )
    (project.path / "conftest.py").write_text(
        """
import pytest

from app import Base, db


@pytest.fixture(scope="session")
def sqlakit_db():
    return db


@pytest.fixture(scope="session")
def sqlakit_metadata():
    return Base.metadata
"""
    )
    project.makepyfile(
        test_plain_order="""
        import pytest

        from app import User, db


        @pytest.fixture
        def a_user():
            db.session.add(User(name="from a fixture"))
            db.session.flush()


        @pytest.fixture(autouse=True)
        def another_user():
            db.session.add(User(name="autouse"))
            db.session.flush()


        @pytest.mark.db
        def test_both_are_visible(a_user):
            assert db.query(User).count() == 2


        @pytest.mark.db
        def test_only_the_autouse_one_is_back(another_user):
            assert [u.name for u in db.query(User).all()] == ["autouse"]
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=2)


def test_a_class_may_seed_rows_of_its_own(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_class_seed="""
        import pytest

        from app import User, db

        pytestmark = pytest.mark.db


        class TestSeeded:
            @pytest.fixture(scope="class", autouse=True)
            def _seeded(self, sqlakit_schema):
                with db.transaction(rollback=True):
                    User(name="ada").save()
                    yield

            def test_sees_the_class_rows(self):
                assert User.query.count() == 1
                User(name="mine").save()

            def test_keeps_them(self):
                assert [u.name for u in User.query.all()] == ["ada"]


        def test_the_class_rows_are_gone():
            assert User.query.count() == 0
        """
    )

    project.runpytest_subprocess().assert_outcomes(passed=3)


def test_a_report_holds_a_card_for_each_marked_test(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_one="""
        import pytest

        from app import User


        @pytest.mark.db
        class TestUsers:
            def test_a_user_can_be_written(self):
                User(name="ada").save()
                assert User.query.count() == 1


        def test_this_one_needs_no_database():
            assert True
        """
    )
    report = project.path / "report.html"
    result = project.runpytest_subprocess(f"--sqlakit-report={report}")

    result.assert_outcomes(passed=2)
    held = report.read_text()

    assert f"sqlakit wrote {report}" in result.stdout.str()
    # The test is the label, the class with it, and the file is the application.
    assert "TestUsers::test_a_user_can_be_written" in held
    assert "test_one.py" in held
    assert "test_this_one_needs_no_database" not in held


def test_a_report_leaves_out_the_queries_a_named_file_runs(
    project: pytest.Pytester,
) -> None:
    (project.path / "factory.py").write_text(
        "from app import User\n\n\ndef make(name):\n    return User(name=name).save()\n"
    )
    (project.path / "pytest.ini").write_text(
        "[pytest]\nsqlakit = true\nsqlakit_skip_queries_from = factory.py\n"
    )
    project.makepyfile(
        test_one="""
        import pytest

        from app import User
        from factory import make


        @pytest.mark.db
        def test_the_report_holds_what_the_test_ran():
            make("ada")
            assert User.query.count() == 1
        """
    )
    report = project.path / "report.html"

    project.runpytest_subprocess(f"--sqlakit-report={report}").assert_outcomes(passed=1)

    held = report.read_text()

    assert "SELECT count" in held
    assert "INSERT INTO users" not in held


def test_a_report_needs_somewhere_to_go(project: pytest.Pytester) -> None:
    result = project.runpytest_subprocess(f"--sqlakit-report={project.path}")

    assert "is a directory" in result.stderr.str() + result.stdout.str()


def test_a_report_names_itself_after_the_clock(project: pytest.Pytester) -> None:
    project.makepyfile(
        test_one="""
        import pytest

        from app import User


        @pytest.mark.db
        def test_a_query_runs():
            assert User.query.count() == 0
        """
    )
    project.runpytest_subprocess("--sqlakit-report").assert_outcomes(passed=1)

    assert list(project.path.glob("sqlakit-*.html"))
