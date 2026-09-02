"""SQL kept in templates, and the rows it comes back with."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound
from typing_extensions import TypedDict  # `typing`'s needs 3.12 to validate

import sqlakit
import sqlakit.asyncio.sql
import sqlakit.sql
from sqlakit import (
    AsyncFilterError,
    Database,
    MissingConnectionError,
    MissingDependencyError,
    RawStatementError,
    SQLNotConfiguredError,
    StrayParameterError,
    TemplateNotFoundError,
)
from sqlakit import _sql as sql_module
from sqlakit.orm import ModelMixin
from sqlakit.sql import Templates

TEMPLATES = {
    "users/active.sql": """
        SELECT * FROM users WHERE team = {{ team }} ORDER BY id
    """,
    "users/by_ids.sql": """
        SELECT name FROM users WHERE id IN {{ ids }} ORDER BY id
    """,
    "users/in_clause.sql": """
        SELECT name FROM users WHERE team IN {{ teams | inclause }} ORDER BY id
    """,
    "users/count.sql": """
        SELECT count(*) FROM users
    """,
    "users/ordered.sql": """
        SELECT name FROM users ORDER BY {{ column | identifier }}
    """,
    "users/one.sql": """
        SELECT name, team FROM users WHERE name = {{ name }}
    """,
    "users/rename_team.sql": """
        UPDATE users SET team = {{ to }} WHERE team = {{ from_ }}
    """,
    "users/since.sql": """
        SELECT name FROM users WHERE joined_at > {{ joined_at }} ORDER BY id
    """,
    "users/dialect.sql": """
        SELECT {{ dialect }} AS dialect
    """,
    "users/all.sql": """
        SELECT name FROM users ORDER BY id
    """,
    "users/cast.sql": """
        SELECT cast(id AS text) || {{ suffix }} FROM users ORDER BY id
    """,
    "hidden/all.sql": """
        SELECT * FROM hidden ORDER BY id
    """,
}


class Base(ModelMixin, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    team: Mapped[str]
    joined_at: Mapped[str]


class TeamMember(BaseModel):
    name: str
    team: str


@dataclass
class TeamMemberRecord:
    name: str
    team: str


class TeamMemberDict(TypedDict):
    name: str
    team: str


@dataclass
class OneName:
    """A row of one column that is still a row, not a value."""

    name: str


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    for name, source in TEMPLATES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


@pytest.fixture
def db(templates: Path) -> Iterator[Database]:
    db = Database(
        "sqlite://", engine_args={"poolclass": sa.StaticPool}, templates=templates
    )
    Base.set_db(db)
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    with db.transaction():
        for index, name in enumerate("abcde"):
            User(
                id=index + 1,
                name=name,
                team="red" if index % 2 else "blue",
                joined_at=f"2026-01-0{index + 1}",
            ).save()
    yield db
    db.dispose()


def test_a_template_reads_rows(db: Database) -> None:
    with db.connect():
        rows = db.sql("users/active.sql", team="red").all()

        assert [row.name for row in rows] == ["b", "d"]


def test_a_value_is_bound_rather_than_written_into_the_sql(db: Database) -> None:
    with db.connect():
        statement = db.sql("users/active.sql", team="red").statement

        assert "'red'" not in str(statement)
        assert ":team" in str(statement)
        assert db.sql("users/active.sql", team="'; DROP TABLE users; --").all() == []


def test_sql_written_out_here(db: Database) -> None:
    with db.connect():
        names = db.sql.from_string(
            "SELECT name FROM users WHERE team = {{ team }} ORDER BY id", team="red"
        )

        assert names.scalars().all() == ["b", "d"]


def test_a_list_is_a_list_to_the_database(db: Database) -> None:
    with db.connect():
        # A bare list expands on this side, `| inclause` on the template's.
        assert db.sql("users/by_ids.sql", ids=[1, 3]).scalars().all() == ["a", "c"]
        assert db.sql("users/in_clause.sql", teams=["red"]).scalars().all() == [
            "b",
            "d",
        ]


def test_a_list_that_is_empty_matches_nothing_rather_than_failing(
    db: Database,
) -> None:
    with db.connect():
        assert db.sql("users/by_ids.sql", ids=[]).all() == []


def test_a_colon_the_database_owns_is_left_alone(db: Database) -> None:
    # `::` is a cast on PostgreSQL, not a parameter, and `text()` knows it.
    with db.connect():
        statement = db.sql.from_string(
            "SELECT name::text FROM users WHERE team = {{ team }}", team="red"
        ).statement

        assert "name::text" in str(statement)
        assert list(cast("sa.TextClause", statement)._bindparams) == ["team__1"]
        assert db.sql("users/cast.sql", suffix="!").scalars().first() == "1!"


def test_a_parameter_can_carry_the_type_the_driver_needs(db: Database) -> None:
    with db.connect():
        joined_at = sa.bindparam("joined_at", "2026-01-03", type_=sa.String())

        rows = db.sql("users/since.sql", joined_at=joined_at).scalars().all()

        assert rows == ["d", "e"]


def test_an_identifier_is_quoted_the_way_the_database_quotes_it(db: Database) -> None:
    # The preparer quotes what has to be quoted, and leaves a plain name alone:
    # a quoted lowercase name is a different column on Oracle.
    with db.connect():
        plain = str(db.sql("users/ordered.sql", column="name").statement)
        mixed = str(db.sql("users/ordered.sql", column="Mixed Name").statement)
        sneaky = str(db.sql("users/ordered.sql", column='a" FROM x --').statement)

        assert "ORDER BY name" in plain
        assert 'ORDER BY "Mixed Name"' in mixed
        assert 'ORDER BY "a"" FROM x --"' in sneaky


def test_a_template_knows_which_database_it_is_written_for(db: Database) -> None:
    with db.connect():
        assert db.sql("users/dialect.sql").scalars().one() == "sqlite"


def test_the_template_says_where_the_sql_came_from(db: Database) -> None:
    with db.recording() as sql, db.connect():
        db.sql("users/count.sql").scalars().one()

    assert "/* users/count.sql */" in str(sql)


def test_sql_written_out_here_carries_no_comment(db: Database) -> None:
    with db.connect():
        assert "/*" not in str(db.sql.from_string("SELECT 1").statement)


@pytest.mark.parametrize("type_", [TeamMember, TeamMemberRecord, TeamMemberDict])
def test_rows_come_back_as_the_type_asked_for(db: Database, type_: Any) -> None:
    with db.connect():
        row = db.sql("users/one.sql", name="b").typed(type_).one()

        assert row == type_(name="b", team="red")


def test_the_type_is_the_row_and_the_terminal_is_the_container(db: Database) -> None:
    with db.connect():
        query = db.sql("users/active.sql", team="red").typed(TeamMember)

        assert query.all() == [
            TeamMember(name="b", team="red"),
            TeamMember(name="d", team="red"),
        ]
        assert query.first() == TeamMember(name="b", team="red")


def test_a_query_says_what_its_rows_are_once(db: Database) -> None:
    with db.connect():
        rows = db.sql("users/active.sql", team="red").typed(TeamMember)

        # Typed rows carry no further say: neither `typed` nor `scalars` is
        # left to call, so no order of the two has to be learned.
        assert not hasattr(rows, "typed")
        assert not hasattr(rows, "scalars")
        assert not hasattr(db.sql("users/count.sql").scalars(), "scalars")


def test_the_type_says_how_much_of_the_row_it_takes(db: Database) -> None:
    with db.connect():
        # Built from a value: the first column of the row.
        assert db.sql("users/count.sql").typed(int).one() == 5
        assert db.sql("users/all.sql").typed(str).all() == list("abcde")

        # Built from columns: the whole row, however few columns it has.
        assert db.sql("users/all.sql").typed(OneName).first() == OneName(name="a")
        assert db.sql("users/all.sql").typed(dict).first() == {"name": "a"}
        assert db.sql("users/one.sql", name="b").typed(TeamMember).one() == TeamMember(
            name="b", team="red"
        )


def test_the_call_is_short_for_reading_a_file(db: Database) -> None:
    with db.connect():
        assert db.sql("users/count.sql").scalars().one() == (
            db.sql.from_file("users/count.sql").scalars().one()
        )


def test_a_statement_built_with_sqlalchemy_is_read_the_same_way(
    db: Database,
) -> None:
    with db.connect():
        statement = sa.text("SELECT name, team FROM users WHERE name = :name")

        rows = db.sql.from_statement(statement.bindparams(name="b"))

        assert rows.typed(TeamMember).one() == TeamMember(name="b", team="red")
        assert db.sql.from_statement(sa.select(sa.literal(1))).typed(int).one() == 1

        # Nothing is rendered, so `{{ }}` is not read and `:name` is the
        # statement's own parameter.
        assert "{{" not in str(rows.statement)


def test_a_filter_that_has_to_be_awaited_is_refused() -> None:
    async def money(value: int) -> str:
        return f"{value}!"

    # Without the check it renders, and binds the coroutine as the value.
    with pytest.raises(AsyncFilterError, match="money"):
        Templates("app/sql", filters={"money": money})

    with pytest.raises(AsyncFilterError, match="rates"):
        Templates("app/sql", globals={"rates": money})


def test_scalars_read_the_first_column(db: Database) -> None:
    with db.connect():
        assert db.sql("users/count.sql").scalars().one() == 5
        assert db.sql("users/all.sql").typed(str).all() == list("abcde")


def test_a_row_that_is_not_there_raises_sqlalchemys_own_error(db: Database) -> None:
    with db.connect():
        with pytest.raises(NoResultFound):
            db.sql("users/one.sql", name="zz").typed(TeamMember).one()

        assert db.sql("users/one.sql", name="zz").one_or_none() is None


def test_more_than_one_row_raises_sqlalchemys_own_error(db: Database) -> None:
    with db.connect():
        with pytest.raises(MultipleResultsFound):
            db.sql("users/all.sql").one()

        with pytest.raises(MultipleResultsFound):
            db.sql("users/all.sql").one_or_none()


def test_chunks_walk_the_whole_result(db: Database) -> None:
    with db.connect():
        batches = [
            [row.name for row in batch] for batch in db.sql("users/all.sql").chunks(2)
        ]

        assert batches == [["a", "b"], ["c", "d"], ["e"]]


def test_chunks_carry_the_shape_the_query_asked_for(db: Database) -> None:
    with db.connect():
        batches = list(db.sql("users/all.sql").scalars().chunks(4))

        assert [list(batch) for batch in batches] == [["a", "b", "c", "d"], ["e"]]


def test_a_template_that_writes_says_how_many_rows_it_touched(db: Database) -> None:
    with db.transaction():
        touched = db.sql("users/rename_team.sql", to="green", from_="red").execute()

        assert touched == 2
        assert db.sql("users/active.sql", team="green").all() != []


def test_a_writing_template_commits_when_no_transaction_is_open(db: Database) -> None:
    # With no transaction to leave the commit to, `execute()` commits for itself.
    with db.connect():
        db.sql("users/rename_team.sql", to="green", from_="red").execute()

    with db.connect():
        assert db.sql("users/active.sql", team="green").all() != []


def test_a_value_is_never_escaped_on_its_way_to_the_database(db: Database) -> None:
    # The templates are Jinja, which escapes what it renders, but a value is
    # bound rather than rendered, so it reaches the database as it was.
    with db.connect():
        value = "a & b <c> 'd'"

        assert (
            db.sql.from_string("SELECT {{ value }}", value=value).scalars().one()
            == value
        )


def test_a_statement_of_your_own_carries_no_query_filter(db: Database) -> None:
    class Hidden(Base):
        __tablename__ = "hidden"

        id: Mapped[int] = mapped_column(primary_key=True)
        deleted: Mapped[bool]

        @classmethod
        def __query_filter__(cls) -> Any:
            return cls.deleted.is_(False)

    with db.transaction() as conn:
        Base.metadata.tables["hidden"].create(conn)
    with db.transaction():
        Hidden(id=1, deleted=False).save()
        Hidden(id=2, deleted=True).save()

    with db.connect():
        assert [row.id for row in Hidden.query.all()] == [1]
        # The SQL is yours, so hiding the row is yours too.
        assert [row.id for row in Hidden.query.from_sql("hidden/all.sql").all()] == [
            1,
            2,
        ]


def test_chunks_come_back_as_the_type_asked_for(db: Database) -> None:
    with db.connect():
        batches = list(
            db.sql("users/active.sql", team="red").typed(TeamMember).chunks(1)
        )

        assert batches == [
            [TeamMember(name="b", team="red")],
            [TeamMember(name="d", team="red")],
        ]


def test_a_colon_the_sql_owns_is_caught_where_it_was_written(db: Database) -> None:
    # `'{"a":1}'` reads as a parameter to SQLAlchemy, and would otherwise fail
    # on execution with nothing pointing at the template.
    with db.connect():
        with pytest.raises(StrayParameterError, match="`:1`"):
            db.sql.from_string("""SELECT '{"a":1}'""").all()

        assert db.sql.from_string(r"""SELECT '{"a"\:1}'""").scalars().one() == '{"a":1}'

        # The other way to reach it: a parameter written as SQLAlchemy binds
        # one, which a template does not.
        with pytest.raises(StrayParameterError, match=r"\{\{ name \}\}"):
            db.sql.from_string("SELECT * FROM users WHERE name = :name", name="b").all()


def test_a_cast_can_follow_a_value(db: Database) -> None:
    # `{{ id }}::uuid` is how a PostgreSQL template is written, and the
    # placeholder has to end where the cast begins.
    with db.connect():
        statement = db.sql.from_string("SELECT {{ n }}::text", n=7).statement

        assert list(cast("sa.TextClause", statement)._bindparams) == ["n__1"]
        assert "::text" in str(statement)


def test_a_template_needs_a_block_and_no_orm(db: Database) -> None:
    # Raw SQL runs on the connection, so the error a reader gets says so.
    with pytest.raises(MissingConnectionError):
        db.sql.from_string("SELECT 1").scalars().one()

    with db.connect():
        assert db.sql.from_string("SELECT 1").scalars().one() == 1


def test_a_template_sees_what_the_block_has_written(db: Database) -> None:
    with db.transaction():
        db.session.add(
            User(id=99, name="f", team="green", joined_at="2026-01-06")
        )  # not flushed

        seen = (
            db.sql.from_string("SELECT name FROM users WHERE id = 99").scalars().all()
        )

        assert seen == ["f"]


def test_a_template_maps_onto_the_model(db: Database) -> None:
    with db.connect():
        users = User.query.from_sql("users/active.sql", team="red").all()

        assert [user.name for user in users] == ["b", "d"]
        assert all(isinstance(user, User) for user in users)


def test_a_statement_stands_in_wherever_sqlalchemy_takes_one(db: Database) -> None:
    with db.connect():
        users = User.query.from_statement(db.sql("users/active.sql", team="red")).all()

        assert [user.name for user in users] == ["b", "d"]

        # The other two doors of `db.sql` map onto the model the same way.
        inline = db.sql.from_string(
            "SELECT * FROM users WHERE team = {{ team }} ORDER BY id", team="red"
        )
        built = sa.text("SELECT * FROM users WHERE name = :name").bindparams(name="b")

        assert [user.name for user in User.query.from_statement(inline).all()] == [
            "b",
            "d",
        ]
        assert [user.name for user in User.query.from_statement(built).all()] == ["b"]


def test_a_query_from_a_template_cannot_be_narrowed(db: Database) -> None:
    with db.connect():
        query = User.query.from_sql("users/active.sql", team="red")

        with pytest.raises(RawStatementError):
            query.where(User.name == "b")


def test_a_template_is_read_from_the_database_the_query_runs_on(
    registry: None, templates: Path
) -> None:
    with sqlakit.db["warehouse"].connect() as conn:
        conn.execute(sa.text("CREATE TABLE users (id int, name text, team text)"))
        conn.execute(sa.text("INSERT INTO users VALUES (1, 'z', 'red')"))

        rows = sqlakit.db["warehouse"].sql("users/active.sql", team="red").all()
        users = User.query.using("warehouse").from_sql("users/active.sql", team="red")

        assert [row.name for row in rows] == ["z"]
        assert [user.name for user in users.all()] == ["z"]


def test_a_template_nobody_has_says_where_it_looked(db: Database) -> None:
    with db.connect():
        with pytest.raises(TemplateNotFoundError, match=r"users/nothing\.sql"):
            db.sql("users/nothing.sql").all()


def test_a_database_without_templates_still_runs_sql_written_out(
    templates: Path,
) -> None:
    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})

    with db.connect():
        assert db.sql.from_string("SELECT 1").scalars().one() == 1

        with pytest.raises(SQLNotConfiguredError):
            db.sql("users/count.sql").all()

        # Checking nowhere would pass, which is worse than saying so.
        with pytest.raises(SQLNotConfiguredError):
            db.sql.check()

    db.dispose()


def test_templates_take_more_than_a_path(templates: Path) -> None:
    db = Database(
        "sqlite://",
        engine_args={"poolclass": sa.StaticPool},
        templates=Templates(
            [templates],
            auto_reload=True,
            globals={"limit": 1},
            filters={"doubled": lambda value: value * 2},
        ),
    )

    with db.connect():
        assert db.sql.from_string("SELECT {{ limit | doubled }}").scalars().one() == 2

    assert str(templates) in repr(db.sql.templates)
    assert repr(db.sql).startswith("SQL(")
    assert repr(db.sql("users/all.sql")) == "SQLQuery('users/all.sql')"
    db.dispose()


def test_a_broken_template_can_be_found_before_it_is_asked_for(
    db: Database, templates: Path
) -> None:
    db.sql.check()

    (templates / "users/broken.sql").write_text("SELECT {% if %}")

    with pytest.raises(sql_module.jinja2.TemplateSyntaxError):
        db.sql.check()


def test_the_adapter_of_a_type_is_built_once(db: Database) -> None:
    with db.connect():
        before = sql_module._adapter.cache_info()
        for _ in range(3):
            db.sql("users/one.sql", name="b").typed(TeamMember).one()
        after = sql_module._adapter.cache_info()

    assert after.misses - before.misses <= 1


def test_a_missing_dependency_says_what_to_install(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sql_module, "TypeAdapter", None)

    with pytest.raises(MissingDependencyError, match="pydantic"):
        db.sql("users/one.sql", name="b").typed(TeamMember)

    monkeypatch.setattr(sql_module, "Jinja2SQL", None)
    fresh = Templates(db.sql.templates.paths)

    with pytest.raises(MissingDependencyError, match=r"sqlakit\[sql\]"):
        fresh.check()


# more than one database


@pytest.fixture
def registry(templates: Path) -> Iterator[None]:
    sqlakit.db.configure(
        {"default": {"url": "sqlite://"}, "warehouse": {"url": "sqlite://"}},
        templates=templates,
    )
    yield
    sqlakit.db.dispose()


def test_every_database_reads_its_own_templates(registry: None) -> None:
    with sqlakit.db["warehouse"].connect():
        assert sqlakit.db["warehouse"].sql.from_string("SELECT 1").scalars().one() == 1

    with sqlakit.db.connect():
        assert sqlakit.db.sql.from_string("SELECT 2").scalars().one() == 2


def test_configuring_again_moves_the_templates(registry: None, tmp_path: Path) -> None:
    assert sqlakit.db.sql.templates.paths == (tmp_path,)

    sqlakit.db.dispose()
    sqlakit.db.configure("sqlite://", templates=tmp_path / "users")

    assert sqlakit.db.sql.templates.paths == (tmp_path / "users",)


@pytest.mark.parametrize(
    ("sync", "asynchronous"),
    [
        (sqlakit.sql.SQL, sqlakit.asyncio.sql.SQL),
        (sqlakit.sql.SQLQuery, sqlakit.asyncio.sql.SQLQuery),
        (sqlakit.sql.SQLRows, sqlakit.asyncio.sql.SQLRows),
    ],
    ids=lambda cls: cls.__name__,
)
def test_the_async_sql_mirrors_this_one(
    sync: type, asynchronous: type, mirrors: Callable[[type, type], None]
) -> None:
    mirrors(sync, asynchronous)


def test_a_value_may_be_named_like_the_argument_before_it(db: Database) -> None:
    # `template` and `source` are ordinary column names, and the API takes
    # every keyword as a value, so neither may be reserved.
    with db.connect():
        rows = db.sql.from_string(
            "SELECT {{ source }} AS source, {{ template }} AS template",
            source="feed",
            template="daily",
        ).all()

        assert [tuple(row) for row in rows] == [("feed", "daily")]
