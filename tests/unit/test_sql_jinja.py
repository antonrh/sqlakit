"""Templates of the default engine, `jinja`: read as SQLAKit 0.20 read them."""

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import jinja2
import pytest
import sqlalchemy as sa
from jinja2sql import Binder
from markupsafe import Markup

from sqlakit import Database
from sqlakit.exceptions import AsyncFilterError, TemplateNotFoundError
from sqlakit.sql import Filter, Templates, sql_macro

TEMPLATES = {
    "users/active.sql": "SELECT name FROM users WHERE team = {{ team }} ORDER BY id",
    "users/in_clause.sql": (
        "SELECT name FROM users WHERE team IN {{ teams | inclause }} ORDER BY id"
    ),
    "users/ordered.sql": "SELECT name FROM users ORDER BY {{ column | identifier }}",
    "users/cast.sql": "SELECT cast(id AS text) || {{ suffix }} FROM users ORDER BY id",
}


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
        "sqlite://",
        engine_args={"poolclass": sa.StaticPool},
        templates=templates,
    )
    with db.connect() as conn:
        conn.execute(sa.text("CREATE TABLE users (id int, name text, team text)"))
        conn.execute(
            sa.text("INSERT INTO users VALUES (1, 'ann', 'red'), (2, 'bob', 'blue')")
        )
        yield db
    db.dispose()


def test_a_jinja_template_reads_rows(db: Database) -> None:
    assert db.sql("users/active.sql", team="red").scalars().all() == ["ann"]
    assert db.sql("users/in_clause.sql", teams=["red", "blue"]).scalars().all() == [
        "ann",
        "bob",
    ]
    assert db.sql("users/ordered.sql", column="name").scalars().all() == ["ann", "bob"]
    assert db.sql("users/cast.sql", suffix="!").scalars().all() == ["1!", "2!"]
    written = db.sql.from_string("SELECT {{ n }}", n=7)
    assert written.scalars().one() == 7


def test_jinja_is_the_engine_unless_another_is_named(templates: Path) -> None:
    assert Templates(templates).engine == "jinja"
    assert Templates(templates, engine="tpl").engine == "tpl"


def test_a_jinja_template_is_checked_as_jinja(templates: Path) -> None:
    (templates / "broken.sql").write_text("SELECT {% if %}")
    with pytest.raises(jinja2.TemplateSyntaxError):
        Templates(templates).check()


def test_a_missing_jinja_template_says_where_it_looked(db: Database) -> None:
    with pytest.raises(TemplateNotFoundError, match=r"users/nothing\.sql"):
        db.sql("users/nothing.sql").all()


def test_the_options_of_one_engine_are_refused_by_the_other() -> None:
    @sql_macro
    def mine() -> str:
        return "TRUE"

    with pytest.raises(ValueError, match="are for templates of `tpl`"):
        Templates("app/sql", macros=[mine])
    with pytest.raises(ValueError, match="are for templates of `tpl`"):
        Templates("app/sql", namespace="q")
    with pytest.raises(ValueError, match="are for templates of Jinja"):
        Templates("app/sql", engine="tpl", filters={"upper": str.upper})
    with pytest.raises(ValueError, match="'tpl' or 'jinja', not 'mako'"):
        Templates("app/sql", engine="mako")  # ty: ignore[invalid-argument-type]


def test_a_filter_that_has_to_be_awaited_is_refused() -> None:
    async def money() -> int:
        return 1

    with pytest.raises(AsyncFilterError, match="money"):
        Templates("app/sql", filters={"money": money})
    with pytest.raises(AsyncFilterError, match="rates"):
        Templates("app/sql", globals={"rates": money})
    with pytest.raises(AsyncFilterError, match="money"):
        Templates("app/sql", filters={"money": Filter(money, bind=True)})


def test_a_bound_filter_writes_sql_and_binds_the_values_in_it(templates: Path) -> None:
    def in_span(binder: Binder, span: tuple[str, str]) -> Markup:
        start, end = span
        return binder.raw(
            f"BETWEEN {binder.bind('span', start)} AND {binder.bind('span', end)}"
        )

    db = Database(
        "sqlite://",
        engine_args={"poolclass": sa.StaticPool},
        templates=Templates(
            templates,
            filters={
                "in_span": Filter(in_span, bind=True),
                "doubled": Filter(lambda value: value * 2),
            },
        ),
    )

    rows = db.sql.from_string(
        "SELECT {{ n | doubled }} WHERE at {{ span | in_span }}",
        n=1,
        span=("2026-01-01", "2026-02-01"),
    )
    statement = cast("sa.TextClause", rows.statement)

    assert str(statement) == "SELECT :n__1  WHERE at BETWEEN :span__2  AND :span__3 "
    assert statement.compile().params == {
        "n__1": 2,
        "span__2": "2026-01-01",
        "span__3": "2026-02-01",
    }
    assert repr(Filter(in_span, bind=True)).startswith("Filter(")
    db.dispose()
