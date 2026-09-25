"""`.tpl.sql` templates: SQL with `tpl.` macros, next to the Jinja ones."""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import default

from sqlakit import (
    Database,
    MacroArgumentError,
    MacroDefinitionError,
    MacroSyntaxError,
    StrayParameterError,
    UnknownIdentifierError,
    UnknownImportPathError,
    UnknownMacroError,
    UnknownOrderFieldError,
)
from sqlakit import _sql as sql_module
from sqlakit.sql import Context, Param, Sql, Templates, sql_macro, tpl

TEMPLATES = {
    "users/list.tpl.sql": """
        SELECT name FROM users
        WHERE
            team IN :teams
            AND tpl.if_set(:search, tpl.icontains(name, :search))
        ORDER BY tpl.order_by(:order_by, id, name, team, 'nulls_last')
        LIMIT :limit
    """,
    "users/for_teams.tpl.sql": """
        SELECT name FROM users WHERE tpl.for_teams(:teams) ORDER BY id
    """,
    "users/in_teams.tpl.sql": """
        SELECT name FROM users WHERE team IN (tpl.each(:teams)) ORDER BY id
    """,
    "users/in_teams.sql": """
        SELECT name FROM users WHERE team IN {{ teams | inclause }} ORDER BY id
    """,
    "users/search.tpl.sql": """
        SELECT name FROM users WHERE tpl.search(:q, name, team) ORDER BY id
    """,
    "users/blue_or.tpl.sql": """
        SELECT name FROM users WHERE tpl.blue_or(:teams) ORDER BY id
    """,
    "users/jinja.sql": """
        SELECT name FROM users WHERE team = {{ team }} ORDER BY id
    """,
}


@sql_macro
def for_teams(teams: Param) -> str:
    """Rows of any of the teams, or none when no team is given."""
    return f"team IN {teams}" if teams.value else "FALSE"


@sql_macro
def json_object(ctx: Context, *pairs: Sql) -> str:
    """JSON_BUILD_OBJECT on PostgreSQL, OBJECT_CONSTRUCT on Snowflake."""
    name = "JSON_BUILD_OBJECT" if ctx.dialect == "postgresql" else "OBJECT_CONSTRUCT"
    return f"{name}({', '.join(pairs)})"


@sql_macro
def search(q: Param, *columns: Sql) -> str:
    """Rows where any of the columns holds the text, regardless of case."""
    if not q.value:
        return "TRUE"
    return " OR ".join(tpl.icontains(column, q) for column in columns)


@sql_macro
def blue_or(teams: Param) -> str:
    """Rows of the blue team, or of any of the teams."""
    return f"team IN ({tpl.each(['blue'])}) OR team IN ({tpl.each(teams)})"


def write(root: Path, templates: dict[str, str]) -> Path:
    for name, source in templates.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    templates = Templates(
        write(tmp_path, TEMPLATES), macros=[for_teams, json_object, search, blue_or]
    )
    db = Database(
        "sqlite://", engine_args={"poolclass": sa.StaticPool}, templates=templates
    )
    with db.transaction() as conn:
        conn.execute(sa.text("CREATE TABLE users (id int, name text, team text)"))
        for index, (name, team) in enumerate(
            [("Ann", "red"), ("bob", "blue"), ("Cid", "red"), ("dan_x", "red")]
        ):
            conn.execute(
                sa.text("INSERT INTO users VALUES (:id, :name, :team)"),
                {"id": index + 1, "name": name, "team": team},
            )
    yield db
    db.dispose()


def names(db: Database, template: str, **values: Any) -> list[str]:
    with db.connect():
        return list(db.sql(template, **values).scalars().all())


def render(source: str, dialect: sa.Dialect, **values: object) -> str:
    """Return what a template becomes on a dialect, parameters left as written."""
    template = sql_module.MacroTemplate(
        "inline.tpl.sql",
        source,
        sql_module.registered([for_teams, json_object, search, blue_or]),
    )
    ctx = Context(dialect.name, dialect.identifier_preparer, values)
    return " ".join(template.render(ctx).split())


LIST: dict[str, Any] = {
    "teams": ["red", "blue"],
    "search": None,
    "order_by": None,
    "limit": 10,
}


def test_a_template_runs_with_nothing_optional(db: Database) -> None:
    assert names(db, "users/list.tpl.sql", **LIST) == ["Ann", "bob", "Cid", "dan_x"]


def test_if_set_keeps_the_condition_when_the_value_is_there(db: Database) -> None:
    values = LIST | {"search": "AN"}
    assert names(db, "users/list.tpl.sql", **values) == ["Ann", "dan_x"]


def test_icontains_matches_a_percent_or_underscore_only_as_itself(
    db: Database,
) -> None:
    assert names(db, "users/list.tpl.sql", **LIST | {"search": "_"}) == ["dan_x"]
    assert names(db, "users/list.tpl.sql", **LIST | {"search": "%"}) == []


def test_order_by_orders_by_sort_strings(db: Database) -> None:
    values = LIST | {"order_by": ["team.desc", "name.desc"]}
    assert names(db, "users/list.tpl.sql", **values) == ["dan_x", "Cid", "Ann", "bob"]


def test_order_by_folds_the_case_convention_of_a_request(db: Database) -> None:
    values = LIST | {"order_by": "Name"}
    assert names(db, "users/list.tpl.sql", **values) == ["Ann", "Cid", "bob", "dan_x"]


def test_order_by_refuses_a_field_it_was_not_given(db: Database) -> None:
    with pytest.raises(UnknownOrderFieldError, match="password"):
        names(db, "users/list.tpl.sql", **LIST | {"order_by": "password"})


def test_order_by_refuses_what_is_not_a_sort_string() -> None:
    with pytest.raises(UnknownOrderFieldError):
        render("ORDER BY tpl.order_by(:o, id)", postgresql.dialect(), o="id.sideways")


def test_order_by_needs_the_columns_it_may_sort_by() -> None:
    with pytest.raises(MacroArgumentError, match="takes at least 2 arguments"):
        render("ORDER BY tpl.order_by(:o)", postgresql.dialect(), o="id")


def test_order_by_sorts_by_an_expression_under_a_name() -> None:
    source = "ORDER BY tpl.order_by(:o, u.id, name = name COLLATE 'und-ci-ai')"
    assert render(source, postgresql.dialect(), o=["name.desc", "id"]) == (
        "ORDER BY name COLLATE 'und-ci-ai' DESC, u.id ASC"
    )


def test_order_by_places_nulls_where_the_template_says_unless_asked() -> None:
    source = "ORDER BY tpl.order_by(:o, id, name, 'nulls_last')"
    assert render(source, postgresql.dialect(), o=["name.asc.nulls_first", "id"]) == (
        "ORDER BY name ASC NULLS FIRST, id ASC NULLS LAST"
    )


def test_order_by_places_nulls_on_mysql_without_nulls_last() -> None:
    source = "ORDER BY tpl.order_by(:o, id, 'nulls_last')"
    assert render(source, mysql.dialect(), o="id.desc") == (
        "ORDER BY id IS NULL ASC, id DESC"
    )


def test_order_by_refuses_nulls_after_the_call() -> None:
    with pytest.raises(MacroArgumentError, match="Pass the default as an argument"):
        render(
            "ORDER BY tpl.order_by(:o, id)\n  nulls last", postgresql.dialect(), o=None
        )


def test_order_by_orders_by_nothing_when_nothing_is_asked() -> None:
    assert render("ORDER BY tpl.order_by(:o, id)", postgresql.dialect(), o=[]) == (
        "ORDER BY (SELECT NULL)"
    )


def test_limit_and_list_parameters_are_bound(db: Database) -> None:
    values = LIST | {"teams": ["blue"], "limit": 1}
    assert names(db, "users/list.tpl.sql", **values) == ["bob"]


def test_an_application_macro_takes_a_parameter(db: Database) -> None:
    assert names(db, "users/for_teams.tpl.sql", teams=["blue"]) == ["bob"]
    assert names(db, "users/for_teams.tpl.sql", teams=[]) == []


def test_a_macro_writes_sql_for_the_dialect() -> None:
    source = "SELECT tpl.json_object('a', 1, 'b', tpl.if_set(:x, 2, 3))"
    assert render(source, postgresql.dialect(), x=1) == (
        "SELECT JSON_BUILD_OBJECT('a', 1, 'b', 2)"
    )


def test_icontains_is_ilike_on_postgres() -> None:
    sql = render("WHERE tpl.icontains(u.name, :q)", postgresql.dialect(), q="a")
    assert sql == "WHERE u.name ILIKE :q__like__1 ESCAPE '!'"


def test_the_layout_of_a_template_survives_rendering(db: Database) -> None:
    with db.connect():
        statement = db.sql("users/list.tpl.sql", **LIST).statement
    assert str(statement) == (
        "/* users/list.tpl.sql */\n"
        "\n"
        "        SELECT name FROM users\n"
        "        WHERE\n"
        "            team IN (__[POSTCOMPILE_teams])\n"
        "            AND TRUE\n"
        "        ORDER BY (SELECT NULL)\n"
        "        LIMIT :limit\n"
        "    "
    )


def test_jinja_templates_render_as_before(db: Database) -> None:
    assert names(db, "users/jinja.sql", team="red") == ["Ann", "Cid", "dan_x"]


def test_calls_in_strings_and_comments_are_text() -> None:
    source = (
        "SELECT 'tpl.nope(', \"tpl.nope(\" -- tpl.nope(\n"
        "/* tpl.nope( */ x.tpl.nope(1), tpl.if_set(:a, 'it''s ) ,', 0)"
    )
    assert render(source, postgresql.dialect(), a=1) == (
        "SELECT 'tpl.nope(', \"tpl.nope(\" -- tpl.nope( "
        "/* tpl.nope( */ x.tpl.nope(1), 'it''s ) ,'"
    )


def test_a_call_may_hold_parentheses_and_commas() -> None:
    source = "tpl.if_set(:a, coalesce(x, (1, 2)), f(y, z))"
    assert render(source, postgresql.dialect(), a=None) == "f(y, z)"


def test_an_unknown_macro_is_refused_when_the_file_is_read() -> None:
    with pytest.raises(UnknownMacroError) as raised:
        render("SELECT 1\nWHERE tpl.foo(:x)", postgresql.dialect(), x=1)
    assert str(raised.value) == (
        "Unknown macro tpl.foo in inline.tpl.sql:2; available: between, blue_or, "
        "each, for_teams, icollate, icontains, identifier, if_set, include, "
        "json_object, order_by, search, unless_set, values. "
        "Register one with "
        "`Templates(..., macros=[...])`."
    )


def test_a_parameter_argument_must_be_a_parameter() -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render("WHERE tpl.if_set(name, TRUE)", postgresql.dialect())
    assert str(raised.value) == (
        "tpl.if_set: argument 1 must be a :parameter, got 'name' in inline.tpl.sql:1."
    )


def test_a_call_with_too_few_arguments_is_refused() -> None:
    with pytest.raises(MacroArgumentError, match="takes 2 to 3 arguments, got 1"):
        render("WHERE tpl.if_set(:a)", postgresql.dialect(), a=1)


def test_a_parameter_the_call_did_not_pass_is_refused() -> None:
    with pytest.raises(MacroArgumentError, match="`:q` was not passed"):
        render("WHERE tpl.icontains(name, :q)", postgresql.dialect())


def test_if_set_reads_a_parameter_the_call_did_not_pass_as_unset() -> None:
    source = "WHERE tpl.if_set(:q, tpl.icontains(name, :q)) AND x = :q"
    assert render(source, postgresql.dialect()) == "WHERE TRUE AND x = :q"


def test_a_parameter_nobody_passed_is_still_refused_outside_if_set() -> None:
    db = Database("sqlite://", templates=Templates(engine="tpl"))
    with pytest.raises(StrayParameterError, match="`:q`"), db.connect():
        db.sql.from_string("SELECT tpl.if_set(:q, 1) WHERE 1 = :q").all()


def test_a_comma_inside_brackets_or_braces_stays_in_the_argument() -> None:
    source = "WHERE tpl.if_set(:x, ARRAY[1, 2] && tags, {'a': 1, 'b': 2})"
    dialect = postgresql.dialect()
    assert render(source, dialect, x=1) == "WHERE ARRAY[1, 2] && tags"
    assert render(source, dialect, x=None) == "WHERE {'a': 1, 'b': 2}"


def test_a_dollar_quoted_string_is_text() -> None:
    source = "SELECT $$ tpl.nope( $$, $fn$ ) tpl.nope( $fn$, a$b$c, tpl.if_set(:a, 1)"
    assert render(source, postgresql.dialect(), a=1) == (
        "SELECT $$ tpl.nope( $$, $fn$ ) tpl.nope( $fn$, a$b$c, 1"
    )


def test_icontains_takes_the_collation_snowflake_compares_under() -> None:
    from sqlalchemy.engine import default

    snowflake = default.DefaultDialect()
    snowflake.name = "snowflake"
    assert render("WHERE tpl.icontains(name, :q, 'en-ci-ai')", snowflake, q="é") == (
        "WHERE CONTAINS(COLLATE(name, 'en-ci-ai'), :q)"
    )


def test_a_coroutine_function_is_not_a_macro() -> None:
    async def later(value: Param) -> str:
        return str(value)

    with pytest.raises(MacroDefinitionError, match="coroutine function"):
        sql_macro(later)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "source",
    ["tpl.if_set(:a, (1)", "SELECT 'open", "SELECT /* open"],
)
def test_what_is_never_closed_is_refused(source: str) -> None:
    with pytest.raises(MacroSyntaxError, match=r"inline\.tpl\.sql:1: .* never closed"):
        render(source, postgresql.dialect(), a=1)


def test_a_function_without_annotations_is_not_a_macro() -> None:
    def untyped(value) -> str:  # noqa: ANN001
        return value

    with pytest.raises(MacroDefinitionError, match="`value` is annotated `None`"):
        sql_macro(untyped)


def test_a_macro_cannot_take_a_builtin_name() -> None:
    @sql_macro(name="if_set")
    def mine(value: Param) -> str:
        return str(value)

    with pytest.raises(MacroDefinitionError, match="another macro has that name"):
        Templates(macros=[mine])


def test_check_reads_every_macro_template(tmp_path: Path) -> None:
    write(tmp_path, {"ok.tpl.sql": "SELECT 1", "bad/one.tpl.sql": "SELECT tpl.foo()"})
    db = Database("sqlite://", templates=tmp_path)
    with pytest.raises(UnknownMacroError, match=r"bad/one\.tpl\.sql:1"):
        db.sql.check()


def test_check_passes_jinja_and_macro_templates(db: Database) -> None:
    db.sql.check()


def test_a_template_outside_the_paths_is_not_found(db: Database) -> None:
    with pytest.raises(FileNotFoundError), db.connect():
        db.sql("../etc/passwd.tpl.sql").all()


def test_auto_reload_reads_a_changed_file(tmp_path: Path) -> None:
    path = write(tmp_path, {"one.tpl.sql": "SELECT 1"}) / "one.tpl.sql"
    db = Database("sqlite://", templates=Templates(tmp_path, auto_reload=True))
    with db.connect():
        assert db.sql("one.tpl.sql").scalars().one() == 1
        path.write_text("SELECT 2")
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 1))
        assert db.sql("one.tpl.sql").scalars().one() == 2


def test_signature_says_how_a_template_calls_a_macro() -> None:
    assert [
        sql_module.signature_of(macro)
        for macro in sql_module.registered([json_object]).values()
    ] == [
        "tpl.if_set(:value, expr[, otherwise])",
        "tpl.unless_set(:value, expr[, otherwise])",
        "tpl.order_by(:sort, column, *columns)",
        "tpl.icontains(column, :text[, collation])",
        "tpl.icollate(column[, collation])",
        "tpl.between(column, :start, :end[, bounds])",
        "tpl.identifier(:name, *allowed)",
        "tpl.each(:values)",
        "tpl.values(:rows)",
        "tpl.json_object(*pairs)",
    ]


def test_the_tpl_engine_renders_every_sql_file_and_string(tmp_path: Path) -> None:
    write(tmp_path, {"plain.sql": "SELECT tpl.if_set(:x, 1, 2)"})
    db = Database("sqlite://", templates=Templates(tmp_path, engine="tpl"))
    with db.connect():
        assert db.sql("plain.sql", x=None).scalars().one() == 2
        assert (
            db.sql.from_string("SELECT tpl.if_set(:x, 1, 2)", x=1).scalars().one() == 1
        )
    db.sql.check()


def test_the_tpl_engine_works_without_the_jinja_extra(tmp_path: Path) -> None:
    write(tmp_path, {"plain.sql": "SELECT tpl.if_set(:x, 1, 2)"})
    script = f"""
import sys
for name in ("jinja2", "jinja2sql", "markupsafe"):
    sys.modules[name] = None
from sqlakit import Database
from sqlakit.sql import Templates
db = Database("sqlite://", templates=Templates({str(tmp_path)!r}, engine="tpl"))
db.sql.check()
with db.connect():
    print(db.sql("plain.sql").scalars().one())
"""
    ran = subprocess.run(  # noqa: S603 - our own interpreter, our own script
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, "2\n", "")


def test_a_macro_template_needs_no_jinja(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sql_module, "Jinja2SQL", None)
    assert names(db, "users/for_teams.tpl.sql", teams=["blue"]) == ["bob"]


def test_an_unknown_engine_is_refused() -> None:
    with pytest.raises(ValueError, match="`engine` is `jinja` or `tpl`"):
        Templates(engine="mako")  # ty: ignore[invalid-argument-type]


def test_the_cli_lists_the_macros_of_a_module(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from sqlakit._cli import main

    assert (
        main(["macros", f"{__name__}:json_object", "--markdown", "--namespace", "q"])
        == 0
    )
    assert capsys.readouterr().out.endswith(
        "### `q.json_object(*pairs)`\n\n"
        "JSON_BUILD_OBJECT on PostgreSQL, OBJECT_CONSTRUCT on Snowflake.\n\n"
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"  # aiosqlite runs on asyncio


@pytest.mark.anyio
async def test_the_async_api_renders_the_same_macros(tmp_path: Path) -> None:
    from sqlakit.asyncio import Database as AsyncDatabase

    write(
        tmp_path,
        {
            "one.tpl.sql": "SELECT team FROM (SELECT 'red' AS team) WHERE tpl.blue_or(:teams)"
        },
    )
    db = AsyncDatabase(
        "sqlite+aiosqlite://", templates=Templates(tmp_path, macros=[blue_or])
    )
    async with db.connect():
        assert await db.sql("one.tpl.sql", teams=["red"]).scalars().all() == ["red"]
        assert await db.sql("one.tpl.sql", teams=["green"]).scalars().all() == []
    await db.dispose()


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("name", "name"),
        ("Mixed Name", '"Mixed Name"'),
        ('say "hi"', '"say ""hi"""'),
        (("reports", "Events"), 'reports."Events"'),
    ],
)
def test_identifier_quotes_as_the_jinja_filter_does(
    tmp_path: Path, value: Any, quoted: str
) -> None:
    write(
        tmp_path,
        {
            "jinja.sql": "SELECT {{ column | identifier }}",
            "macro.tpl.sql": "SELECT tpl.identifier(:column)",
        },
    )
    db = Database("postgresql+psycopg://", templates=tmp_path)
    jinja = str(db.sql("jinja.sql", column=value).statement).splitlines()[-1]
    macro = str(db.sql("macro.tpl.sql", column=value).statement).splitlines()[-1]
    assert jinja == macro == f"SELECT {quoted}"


def test_identifier_takes_only_the_names_it_lists() -> None:
    source = "SELECT tpl.identifier(:c, a.id, fans = fans_count)"
    dialect = postgresql.dialect()
    assert render(source, dialect, c="fans") == "SELECT fans_count"
    assert render(source, dialect, c="ID") == "SELECT a.id"
    with pytest.raises(UnknownIdentifierError, match="It takes: fans, id"):
        render(source, dialect, c="password")


@pytest.mark.parametrize("value", [None, "", (), ("a", "")])
def test_identifier_refuses_an_empty_name(value: Any) -> None:
    with pytest.raises(UnknownIdentifierError):
        render("SELECT tpl.identifier(:c)", postgresql.dialect(), c=value)


def test_each_binds_each_value_as_the_jinja_filter_does(db: Database) -> None:
    source = "WHERE team IN (tpl.each(:teams))"
    assert render(source, postgresql.dialect(), teams=["red", "blue"]) == (
        "WHERE team IN (:teams__1, :teams__2)"
    )
    assert names(db, "users/in_teams.tpl.sql", teams=["blue"]) == ["bob"]
    assert names(db, "users/in_teams.sql", teams=["blue"]) == ["bob"]


def test_each_refuses_an_empty_list() -> None:
    with pytest.raises(MacroArgumentError, match="`:teams` is empty"):
        render("WHERE team IN (tpl.each(:teams))", postgresql.dialect(), teams=[])


def test_a_namespace_replaces_tpl_where_a_schema_has_that_name(tmp_path: Path) -> None:
    write(tmp_path, {"one.tpl.sql": "SELECT q.if_set(:x, 1, 2), tpl.f(1) FROM tpl.t"})
    db = Database(
        "sqlite://",
        templates=Templates(tmp_path, namespace="q", macros=[for_teams]),
    )
    statement = db.sql("one.tpl.sql", x=None).statement
    assert str(statement).splitlines()[-1] == "SELECT 2, tpl.f(1) FROM tpl.t"


def test_errors_name_the_namespace_in_use() -> None:
    template = "WHERE q.nope(:x)"
    with pytest.raises(UnknownMacroError, match=r"Unknown macro q\.nope in <string>"):
        sql_module.MacroTemplate(
            "<string>", template, sql_module.registered([]), 0, "q"
        )


def test_a_namespace_is_a_plain_name() -> None:
    with pytest.raises(ValueError, match="`namespace` is a plain name"):
        Templates(namespace="my schema")


def test_a_macros_own_refusal_says_where_the_call_is() -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render(
            "SELECT 1\nWHERE x IN (tpl.each(:teams))",
            postgresql.dialect(),
            teams=[],
        )
    assert str(raised.value) == (
        "tpl.each: `:teams` is empty, and `IN ()` is not SQL in inline.tpl.sql:2."
    )


def test_a_macro_calls_a_builtin_one_as_a_template_would(db: Database) -> None:
    assert names(db, "users/search.tpl.sql", q="BLU") == ["bob"]
    assert names(db, "users/search.tpl.sql", q="") == ["Ann", "bob", "Cid", "dan_x"]
    assert render("WHERE tpl.search(:q, a, b)", postgresql.dialect(), q="x") == (
        "WHERE a ILIKE :q__like__1 ESCAPE '!' OR b ILIKE :q__like__2 ESCAPE '!'"
    )


def test_a_value_where_a_parameter_goes_is_bound(db: Database) -> None:
    assert names(db, "users/blue_or.tpl.sql", teams=["red"]) == [
        "Ann",
        "bob",
        "Cid",
        "dan_x",
    ]
    assert render("WHERE tpl.blue_or(:teams)", postgresql.dialect(), teams=["red"]) == (
        "WHERE team IN (:__p1) OR team IN (:teams__2)"
    )


def test_a_macro_that_binds_cannot_be_called_outside_a_template() -> None:
    with pytest.raises(
        MacroArgumentError, match=r"tpl\.each: called outside a template"
    ):
        tpl.each([1])


def test_a_macro_that_only_writes_sql_can() -> None:
    assert tpl.if_set(Param("q", None), "x = 1") == "TRUE"


@pytest.mark.parametrize(
    ("values", "sql"),
    [
        ({}, "WHERE status <> 'archived'"),
        ({"status": None}, "WHERE status <> 'archived'"),
        ({"status": []}, "WHERE status <> 'archived'"),
        ({"status": "open"}, "WHERE TRUE"),
        ({"status": 0}, "WHERE TRUE"),
    ],
)
def test_unless_set_applies_only_when_the_value_is_not_there(
    values: dict[str, Any], sql: str
) -> None:
    source = "WHERE tpl.unless_set(:status, status <> 'archived')"
    assert render(source, postgresql.dialect(), **values) == sql


def test_unless_set_takes_what_to_write_when_the_value_is_there() -> None:
    source = "WHERE tpl.unless_set(:ids, FALSE, id IN :ids)"
    assert render(source, postgresql.dialect(), ids=[1]) == "WHERE id IN :ids"
    assert render(source, postgresql.dialect(), ids=[]) == "WHERE FALSE"


def test_a_macro_names_the_values_it_binds() -> None:
    ctx = Context("postgresql", postgresql.dialect().identifier_preparer, {"x__1": 0})
    assert [ctx.bind(1, "x"), ctx.bind(2), ctx.bind(3, "x")] == [
        ":x__2",
        ":__p3",
        ":x__4",
    ]
    assert ctx.values == {"x__1": 0, "x__2": 1, "__p3": 2, "x__4": 3}


INCLUDES = {
    "fans/ids.tpl.sql": """SELECT id FROM users
WHERE team IN :teams AND tpl.if_set(:q, tpl.icontains(name, :q));
""",
    "fans/count.tpl.sql": (
        "SELECT count(*) FROM tpl.include('fans/ids.tpl.sql') AS f\n"
        "WHERE tpl.icontains('x', :q) OR TRUE"
    ),
    "fans/names.tpl.sql": """SELECT u.name
FROM users AS u
JOIN tpl.include('fans/ids.tpl.sql') AS f ON f.id = u.id
ORDER BY u.id""",
    "loop/a.tpl.sql": "SELECT * FROM tpl.include('loop/b.tpl.sql') AS b",
    "loop/b.tpl.sql": "SELECT * FROM tpl.include('loop/a.tpl.sql') AS a",
    "broken/outer.tpl.sql": "SELECT 1\nFROM tpl.include('broken/inner.tpl.sql') AS i",
    "broken/inner.tpl.sql": "SELECT 1\nWHERE tpl.icontains(name, :missing)",
    "jinja/plain.sql": "SELECT {{ 1 }}",
    "jinja/includes_tpl.sql": "{% include 'fans/ids.tpl.sql' %}",
}


@pytest.fixture
def included(tmp_path: Path, db: Database) -> Database:
    db.templates = Templates(write(tmp_path, TEMPLATES | INCLUDES))
    return db


def test_include_puts_a_whole_query_in_place(included: Database) -> None:
    assert names(included, "fans/names.tpl.sql", teams=["red"], q="n") == [
        "Ann",
        "dan_x",
    ]
    with included.connect():
        count = included.sql("fans/count.tpl.sql", teams=["red", "blue"], q=None)
        assert count.scalars().one() == 4


def test_include_writes_the_query_in_parentheses_labelled_once(
    included: Database,
) -> None:
    with included.connect():
        statement = included.sql("fans/count.tpl.sql", teams=["red"], q="a").statement
    assert str(statement) == (
        "/* fans/count.tpl.sql */\n"
        "SELECT count(*) FROM (SELECT id FROM users\n"
        "WHERE team IN (__[POSTCOMPILE_teams]) AND lower(name) LIKE "
        "lower(:q__like__1) ESCAPE '!') AS f\n"
        "WHERE lower('x') LIKE lower(:q__like__2) ESCAPE '!' OR TRUE"
    )


def test_include_refuses_a_template_that_includes_itself(included: Database) -> None:
    with pytest.raises(MacroArgumentError) as raised:
        included.sql("loop/a.tpl.sql").statement
    assert str(raised.value) == (
        "tpl.include: includes itself: loop/a.tpl.sql -> loop/b.tpl.sql -> "
        "loop/a.tpl.sql in loop/b.tpl.sql:1 (included from loop/a.tpl.sql:1)."
    )


def test_check_finds_what_an_include_breaks(included: Database) -> None:
    with pytest.raises(MacroArgumentError, match="includes itself"):
        included.sql.check()


def test_an_error_in_an_included_template_says_where_it_was_included(
    included: Database,
) -> None:
    with pytest.raises(MacroArgumentError) as raised, included.connect():
        included.sql("broken/outer.tpl.sql").all()
    assert str(raised.value) == (
        "tpl.icontains: `:missing` was not passed in broken/inner.tpl.sql:2 "
        "(included from broken/outer.tpl.sql:2)."
    )


def test_a_missing_included_template_is_refused_on_load(tmp_path: Path) -> None:
    write(
        tmp_path, {"outer.tpl.sql": "SELECT 1\nFROM tpl.include('gone.tpl.sql') AS g"}
    )
    db = Database("sqlite://", templates=tmp_path)
    with pytest.raises(
        MacroArgumentError,
        match=r"No SQL template named `gone\.tpl\.sql`.* in outer\.tpl\.sql:2\.",
    ):
        db.sql.check()


@pytest.mark.parametrize(
    ("argument", "written"),
    [
        (":name", "':name'"),
        ("'a.tpl.sql', 'b.tpl.sql'", "\"'a.tpl.sql', 'b.tpl.sql'\""),
    ],
)
def test_include_takes_one_path_written_out(argument: str, written: str) -> None:
    with pytest.raises(MacroArgumentError) as raised:
        source = "SELECT * FROM tpl.include(" + argument + ") AS x"  # noqa: S608
        render(source, postgresql.dialect())
    assert str(raised.value) == (
        "tpl.include: takes the path of a template as a string, such as "
        f"'reports/ids.tpl.sql', got {written} in inline.tpl.sql:1."
    )


def test_include_refuses_a_jinja_template(included: Database, tmp_path: Path) -> None:
    write(
        tmp_path, {"outer.tpl.sql": "SELECT * FROM tpl.include('jinja/plain.sql') AS j"}
    )
    with pytest.raises(
        MacroArgumentError, match=r"`jinja/plain\.sql` is a Jinja template"
    ):
        included.sql("outer.tpl.sql").statement


def test_the_tpl_engine_includes_a_plain_sql_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "one.sql": "SELECT 1 AS n;",
            "two.sql": "SELECT n FROM tpl.include('one.sql') AS o",
        },
    )
    db = Database("sqlite://", templates=Templates(tmp_path, engine="tpl"))
    with db.connect():
        assert db.sql("two.sql").scalars().one() == 1


def test_jinja_refuses_to_include_a_macro_template(included: Database) -> None:
    with pytest.raises(
        Exception, match="is a macro template, which Jinja cannot include"
    ):
        included.sql("jinja/includes_tpl.sql", teams=[], q=None).statement


def test_include_is_not_a_name_a_macro_can_take() -> None:
    @sql_macro(name="include")
    def mine(value: Param) -> str:
        return str(value)

    with pytest.raises(MacroDefinitionError, match="another macro has that name"):
        Templates(macros=[mine])


def test_auto_reload_reads_a_changed_included_file(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "inner.tpl.sql": "SELECT 1 AS n",
            "outer.tpl.sql": "SELECT n FROM tpl.include('inner.tpl.sql') AS i",
        },
    )
    db = Database("sqlite://", templates=Templates(tmp_path, auto_reload=True))
    with db.connect():
        assert db.sql("outer.tpl.sql").scalars().one() == 1
        path = tmp_path / "inner.tpl.sql"
        path.write_text("SELECT 2 AS n")
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + 1))
        assert db.sql("outer.tpl.sql").scalars().one() == 2


def snowflake() -> sa.Dialect:
    dialect = default.DefaultDialect()
    dialect.name = "snowflake"
    return dialect


def mariadb() -> sa.Dialect:
    dialect = mysql.dialect()
    dialect.name = "mariadb"
    return dialect


# icollate


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (snowflake(), "ORDER BY name COLLATE 'und-ci-ai'"),
        (postgresql.dialect(), "ORDER BY lower(name)"),
        (sqlite.dialect(), "ORDER BY lower(name)"),
        (mysql.dialect(), "ORDER BY name"),
        (mariadb(), "ORDER BY name"),
    ],
)
def test_icollate_compares_without_case_on_each_dialect(
    dialect: sa.Dialect, sql: str
) -> None:
    assert render("ORDER BY tpl.icollate(name, 'und-ci-ai')", dialect) == sql


def test_icollate_is_en_ci_on_snowflake_unless_told() -> None:
    source = "WHERE tpl.icollate(email) = tpl.icollate(:email)"
    assert render(source, snowflake(), email="A") == (
        "WHERE email COLLATE 'en-ci' = :email COLLATE 'en-ci'"
    )


def test_icollate_sorts_under_order_by_without_case(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "sorted.tpl.sql": "SELECT name FROM (SELECT 'b' AS name UNION ALL "
            "SELECT 'a' UNION ALL SELECT 'C') AS t "
            "ORDER BY tpl.order_by(:sort, name = tpl.icollate(name))"
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    with db.connect():
        rows = db.sql("sorted.tpl.sql", sort="name").scalars().all()
        plain = db.sql.from_string(
            "SELECT name FROM (SELECT 'b' AS name UNION ALL SELECT 'a' "
            "UNION ALL SELECT 'C') AS t ORDER BY name"
        )
        assert plain.scalars().all() == ["C", "a", "b"]
    assert rows == ["a", "b", "C"]


# between


@pytest.mark.parametrize(
    ("start", "end", "closed", "half_open"),
    [
        (1, 9, "d BETWEEN :s AND :e", "(d >= :s AND d < :e)"),
        (1, None, "d >= :s", "d >= :s"),
        (None, 9, "d <= :e", "d < :e"),
        (None, None, "TRUE", "TRUE"),
        (0, "", "d >= :s", "d >= :s"),
    ],
)
def test_between_writes_what_the_ends_given_allow(
    start: Any, end: Any, closed: str, half_open: str
) -> None:
    dialect = postgresql.dialect()
    assert render("tpl.between(d, :s, :e)", dialect, s=start, e=end) == closed
    assert render("tpl.between(d, :s, :e, '[)')", dialect, s=start, e=end) == half_open


def test_between_reads_an_end_not_passed_as_missing() -> None:
    assert render("tpl.between(d, :s, :e)", postgresql.dialect(), s=1) == "d >= :s"


def test_between_refuses_other_bounds_when_the_file_is_read() -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render("WHERE tpl.between(d, :s, :e, '(]')", postgresql.dialect())
    assert str(raised.value) == (
        "tpl.between: argument 4 is '[]' or '[)', got '(]' in inline.tpl.sql:1."
    )


# values


def test_values_writes_a_table_one_parameter_per_value() -> None:
    source = "SELECT * FROM tpl.values(:rows) AS v"
    rows = [(1, "a"), (2, "b")]
    assert render(source, postgresql.dialect(), rows=rows) == (
        "SELECT * FROM (VALUES (:rows__1, :rows__2), (:rows__3, :rows__4)) AS v"
    )
    assert render(source, mysql.dialect(), rows=rows) == (
        "SELECT * FROM (SELECT :rows__1 AS column1, :rows__2 AS column2 "
        "UNION ALL SELECT :rows__3, :rows__4) AS v"
    )
    assert render(source, mariadb(), rows=[1]) == (
        "SELECT * FROM (SELECT :rows__1 AS column1) AS v"
    )


def test_values_runs_as_a_table(tmp_path: Path) -> None:
    write(
        tmp_path,
        {
            "segments.tpl.sql": "SELECT column1, column2 FROM tpl.values(:segments) "
            "AS v ORDER BY column1"
        },
    )
    db = Database("sqlite://", templates=tmp_path)
    with db.connect():
        rows = db.sql("segments.tpl.sql", segments=[[2, "b"], (1, "a")]).all()
    assert [tuple(row) for row in rows] == [(1, "a"), (2, "b")]


@pytest.mark.parametrize(
    ("rows", "problem"),
    [
        ([], "`:rows` has no rows, and `VALUES` without one is not SQL"),
        ([(1, 2), (3,)], "row 2 of `:rows` has 1 values, and row 1 has 2"),
    ],
)
def test_values_refuses_what_is_not_a_table(rows: Any, problem: str) -> None:
    with pytest.raises(MacroArgumentError) as raised:
        render("SELECT * FROM tpl.values(:rows) AS v", postgresql.dialect(), rows=rows)
    assert str(raised.value) == f"tpl.values: {problem} in inline.tpl.sql:1."


def test_a_literal_annotation_names_the_sql_an_argument_may_be() -> None:
    from typing import Literal

    @sql_macro
    def pick(which: Literal["'a'", "'b'"]) -> str:
        return which

    assert pick.slots[0].choices == ("'a'", "'b'")


def test_macros_are_imported_from_a_path() -> None:
    assert set(Templates(macros=[__name__]).macros) - set(
        sql_module.BUILTIN_MACROS
    ) == {
        "blue_or",
        "for_teams",
        "json_object",
        "search",
    }
    for path in (f"{__name__}:search", f"{__name__}.search"):
        assert Templates(macros=[path]).macros["search"] is search


def test_a_path_to_something_else_is_not_a_macro() -> None:
    with pytest.raises(MacroDefinitionError, match="it is not decorated @sql_macro"):
        Templates(macros=[f"{__name__}:write"])


def test_a_path_that_names_nothing_is_refused() -> None:
    with pytest.raises(UnknownImportPathError):
        Templates(macros=[f"{__name__}:nope"])
