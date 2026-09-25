"""`.tpl.sql` templates: SQL with `tpl.` macros, next to the Jinja ones."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from sqlakit import (
    Database,
    MacroArgumentError,
    MacroDefinitionError,
    MacroSyntaxError,
    UnknownMacroError,
    UnknownOrderFieldError,
)
from sqlakit import _sql as sql_module
from sqlakit.sql import Context, Param, Sql, Templates, sql_macro

TEMPLATES = {
    "users/list.tpl.sql": """
        SELECT name FROM users
        WHERE
            team IN :teams
            AND tpl.if_set(:search, tpl.ci_contains(name, :search))
        ORDER BY tpl.sort_by(:order_by, id, name, team) NULLS LAST
        LIMIT :limit
    """,
    "users/for_teams.tpl.sql": """
        SELECT name FROM users WHERE tpl.for_teams(:teams) ORDER BY id
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


def write(root: Path, templates: dict[str, str]) -> Path:
    for name, source in templates.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return root


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    templates = Templates(write(tmp_path, TEMPLATES), macros=[for_teams, json_object])
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
        "inline.tpl.sql", source, sql_module.registered([for_teams, json_object])
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


def test_ci_contains_matches_a_percent_or_underscore_only_as_itself(
    db: Database,
) -> None:
    assert names(db, "users/list.tpl.sql", **LIST | {"search": "_"}) == ["dan_x"]
    assert names(db, "users/list.tpl.sql", **LIST | {"search": "%"}) == []


def test_sort_by_orders_by_sort_strings(db: Database) -> None:
    values = LIST | {"order_by": ["team.desc", "name.desc"]}
    assert names(db, "users/list.tpl.sql", **values) == ["dan_x", "Cid", "Ann", "bob"]


def test_sort_by_folds_the_case_convention_of_a_request(db: Database) -> None:
    values = LIST | {"order_by": "Name"}
    assert names(db, "users/list.tpl.sql", **values) == ["Ann", "Cid", "bob", "dan_x"]


def test_sort_by_refuses_a_field_it_was_not_given(db: Database) -> None:
    with pytest.raises(UnknownOrderFieldError, match="password"):
        names(db, "users/list.tpl.sql", **LIST | {"order_by": "password"})


def test_sort_by_refuses_sql_in_place_of_a_name() -> None:
    dialect = postgresql.dialect()
    with pytest.raises(UnknownOrderFieldError):
        render("ORDER BY tpl.sort_by(:o)", dialect, o="id; DROP TABLE users")
    with pytest.raises(UnknownOrderFieldError):
        render("ORDER BY tpl.sort_by(:o)", dialect, o="id.sideways")


def test_sort_by_quotes_a_name_when_no_columns_are_listed() -> None:
    sql = render(
        "ORDER BY tpl.sort_by(:o)", postgresql.dialect(), o=["Name.desc.nulls_last"]
    )
    assert sql == 'ORDER BY "Name" DESC NULLS LAST'


def test_sort_by_orders_by_nothing_when_nothing_is_asked() -> None:
    assert render("ORDER BY tpl.sort_by(:o)", postgresql.dialect(), o=[]) == (
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


def test_ci_contains_is_ilike_on_postgres() -> None:
    sql = render("WHERE tpl.ci_contains(u.name, :q)", postgresql.dialect(), q="a")
    assert sql == "WHERE u.name ILIKE :__p1 ESCAPE '!'"


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
        "        ORDER BY (SELECT NULL) NULLS LAST\n"
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
        "Unknown macro tpl.foo in inline.tpl.sql:2; available: ci_contains, "
        "for_teams, if_set, json_object, sort_by. Register one with "
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
    with pytest.raises(MacroArgumentError, match="`:a` was not passed"):
        render("WHERE tpl.if_set(:a, TRUE)", postgresql.dialect())


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
        "tpl.sort_by(:order_by, *allowed)",
        "tpl.ci_contains(column, :text)",
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


def test_the_tpl_engine_never_imports_jinja(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sql_module, "Jinja2SQL", None)
    write(tmp_path, {"plain.sql": "SELECT 1"})
    db = Database("sqlite://", templates=Templates(tmp_path, engine="tpl"))
    with db.connect():
        assert db.sql("plain.sql").scalars().one() == 1
    db.sql.check()


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

    assert main(["macros", f"{__name__}:json_object", "--markdown"]) == 0
    assert capsys.readouterr().out.endswith(
        "### `tpl.json_object(*pairs)`\n\n"
        "JSON_BUILD_OBJECT on PostgreSQL, OBJECT_CONSTRUCT on Snowflake.\n\n"
    )
