"""`sqlakit check` and `sqlakit lsp`: a project's templates, read from pyproject.toml."""

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

from sqlakit import ProjectConfigError
from sqlakit._cli import main
from sqlakit._sql import signature_of, sql_macros
from sqlakit.editor._lsp import (
    Completion,
    Diagnostic,
    Target,
    _Assistant,
    _source_of,
    _utf16_column,
    offset_of,
    position_of,
)
from sqlakit.editor._project import load_project

PYPROJECT = """
[project]
name = "app"

[tool.sqlakit.templates]
paths = ["sql"]
macros = ["lsp_macros", "_macros.sql"]
"""

MACROS = '''
from sqlakit.sql import Param, sql_macro


@sql_macro
def mine(teams: Param) -> str:
    """Rows of any of the teams."""
    return f"team IN {teams}"
'''

SQL_MACROS = """-- Rows of the team the call asks for.
SELECT t.team = :team AS for_team FROM t;

SELECT tpl.for_team(t) OR t.public AS visible FROM t;
"""

TEMPLATES = {
    "good.tpl.sql": "SELECT * FROM users\nWHERE tpl.mine(:teams)\n  AND tpl.if_set(:q, name = :q)",
    "inner.tpl.sql": "SELECT 1\nWHERE tpl.nope(:x)",
    "outer.tpl.sql": "SELECT *\nFROM tpl.include('inner.tpl.sql') AS i",
    "open.sql": "SELECT 1,\n  'never closed",
}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "lsp_macros.py").write_text(MACROS)
    (tmp_path / "_macros.sql").write_text(SQL_MACROS)
    for name, source in TEMPLATES.items():
        path = tmp_path / "sql" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "lsp_macros", raising=False)
    return tmp_path


@pytest.fixture
def assistant(project: Path) -> _Assistant:
    return _Assistant(load_project(project))


# the project


def test_a_project_reads_its_templates_from_pyproject(project: Path) -> None:
    loaded = load_project(project / "sql")
    assert loaded.root == project
    assert loaded.templates.paths == (project / "sql",)
    assert "mine" in loaded.templates.macros


@pytest.mark.parametrize(
    ("pyproject", "problem"),
    [
        (None, "no code under"),
        ("[project]\nname = 'app'\n", "no code under"),
        (
            "[tool.sqlakit.templates]\npath = ['sql']\n",
            "has path, and takes dialect, macros, namespace, paths",
        ),
    ],
)
def test_a_project_with_no_templates_to_find_is_refused(
    tmp_path: Path, pyproject: str | None, problem: str
) -> None:
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
    with pytest.raises(ProjectConfigError, match=re.escape(problem)):
        load_project(tmp_path)


APP = {
    "pyproject.toml": "[project]\nname = 'shop'\n",
    "shop/__init__.py": "",
    "shop/db.py": """
from pathlib import Path

from sqlakit import Database
from sqlakit.sql import Templates

BASE_DIR = Path(__file__).parent / "sql"

db = Database(
    "sqlite://",
    templates=Templates(
        BASE_DIR,
        macros=["shop.macros", BASE_DIR / "_macros.sql"],
        namespace="q",
    ),
)
raise RuntimeError("imported")
""",
    "shop/macros.py": '''
from typing import Literal

from sqlakit.sql import Context, Param, Sql, sql_macro


@sql_macro(optional=True)
def owned(ctx: Context, team: Param, *columns: Sql) -> str:
    """Rows of the team."""
    raise RuntimeError("called")


@sql_macro(name="sided")
def side(which: Literal["'left'", "'right'"] = "'left'") -> str:
    return which
''',
    "shop/sql/_macros.sql": "-- Rows of the team.\nSELECT t.team = :team AS for_team FROM t;\n",
    "shop/sql/users.sql": "SELECT * FROM users AS u WHERE q.owned(:team, u.a) AND q.for_team(u)\n",
    "tests/test_it.py": "from sqlakit.sql import Templates\nTemplates('elsewhere')\n",
}


@pytest.fixture
def app(tmp_path: Path) -> Path:
    for name, source in APP.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def test_a_project_is_read_from_its_code_without_running_it(app: Path) -> None:
    project = load_project(app / "shop")
    templates = project.templates
    assert (project.root, templates.paths, templates.namespace) == (
        app,
        (app / "shop" / "sql",),
        "q",
    )
    assert [
        signature_of(templates.macros[name], "q")
        for name in ("owned", "sided", "for_team")
    ] == [
        "q.owned(:team, *columns)",
        "q.sided([which])",
        "q.for_team(t)",
    ]
    assert templates.macros["owned"].doc == "Rows of the team."
    assert templates.macros["owned"].optional
    assert templates.macros["sided"].slots[0].choices == ("'left'", "'right'")
    assert "shop.macros" not in sys.modules
    assert "shop.db" not in sys.modules


def test_a_template_found_in_the_code_is_checked(app: Path) -> None:
    helper = _Assistant(load_project(app))
    path = app / "shop" / "sql" / "users.sql"
    assert helper.diagnose(path, path.read_text()) == []
    [found] = helper.diagnose(path, "SELECT q.sided('up')")
    assert found.message == "q.sided: argument 1 is 'left' or 'right', got 'up'"
    assert helper.definition("WHERE q.owned(:t)", 8) == Target(
        app / "shop" / "macros.py", 7, 4
    )


def test_the_sql_directories_stand_in_for_code_that_says_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / "app" / "sql").mkdir(parents=True)
    (tmp_path / "tests" / "sql").mkdir(parents=True)
    assert load_project(tmp_path).templates.paths == (tmp_path / "app" / "sql",)


# sqlakit check


def test_check_names_every_problem_once_where_it_is(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    available = ", ".join(sorted([*load_project(project).templates.macros, "include"]))
    assert main(["check"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        (
            "sql/inner.tpl.sql:2:7: Unknown macro tpl.nope in inner.tpl.sql:2; "
            f"available: {available}. Register one with "
            "`Templates(..., macros=[...])`."
        ),
        "sql/open.sql:2:3: open.sql:2: a quoted string is never closed.",
        "4 templates, 2 problems",
    ]


def test_check_writes_json(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--format", "json"]) == 1
    found = json.loads(capsys.readouterr().out)
    assert [(one["path"], one["line"], one["column"]) for one in found] == [
        ("sql/inner.tpl.sql", 2, 7),
        ("sql/open.sql", 2, 3),
    ]


def test_check_names_a_missing_include_where_the_include_is(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "sql" / "inner.tpl.sql").unlink()
    (project / "sql" / "open.sql").unlink()
    assert main(["check"]) == 1
    assert (
        capsys.readouterr()
        .out.splitlines()[0]
        .startswith(
            "sql/outer.tpl.sql:2:6: tpl.include: No SQL template named `inner.tpl.sql`"
        )
    )


def test_check_passes_a_clean_project(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("inner.tpl.sql", "outer.tpl.sql", "open.sql"):
        (project / "sql" / name).unlink()
    assert main(["check"]) == 0
    assert capsys.readouterr().out == "1 templates, 0 problems\n"


def test_check_says_when_the_project_says_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--project", str(tmp_path)]) == 2
    assert "Cannot read the project's templates" in capsys.readouterr().out


# problems as you type


def test_a_good_template_has_no_problems(assistant: _Assistant, project: Path) -> None:
    path = project / "sql" / "good.tpl.sql"
    assert assistant.diagnose(path, path.read_text()) == []


def test_an_unknown_macro_is_marked_where_the_call_is(
    assistant: _Assistant, project: Path
) -> None:
    source = "SELECT 1\nWHERE tpl.nope(:x) AND TRUE"
    [found] = assistant.diagnose(project / "sql" / "new.tpl.sql", source)
    assert source[found.start : found.end] == "tpl.nope(:x)"
    assert found.message.startswith("unknown macro tpl.nope; available: ")


def test_an_argument_is_marked_where_it_is(
    assistant: _Assistant, project: Path
) -> None:
    source = "WHERE tpl.mine( teams )"
    [found] = assistant.diagnose(project / "sql" / "new.tpl.sql", source)
    assert found == Diagnostic(
        16, 21, "tpl.mine: argument 1 must be a :parameter, got 'teams'"
    )


def test_what_is_never_closed_is_marked_where_it_opens(
    assistant: _Assistant, project: Path
) -> None:
    source = "SELECT 1,\n  'open"
    [found] = assistant.diagnose(project / "sql" / "new.tpl.sql", source)
    assert (found.start, found.message) == (12, "a quoted string is never closed")


def test_a_problem_in_an_included_template_is_marked_on_the_include(
    assistant: _Assistant, project: Path
) -> None:
    path = project / "sql" / "outer.tpl.sql"
    source = path.read_text()
    [found] = assistant.diagnose(path, source)
    assert source[found.start : found.end] == "FROM tpl.include('inner.tpl.sql') AS i"
    assert "(included from outer.tpl.sql:2)" in found.message


def test_the_server_reads_the_sql_files_under_the_paths(
    assistant: _Assistant, project: Path
) -> None:
    assert assistant.applies_to(project / "sql" / "good.tpl.sql")
    assert assistant.applies_to(project / "sql" / "open.sql")
    assert not assistant.applies_to(project / "sql" / "notes.txt")
    assert not assistant.applies_to(project / "elsewhere.sql")


# completion, hover, definition


def test_macros_complete_after_the_namespace(assistant: _Assistant) -> None:
    source = "WHERE tpl.i"
    labels = [one.label for one in assistant.complete(source, len(source))]
    assert labels == [
        "if_set",
        "icontains",
        "icollate",
        "identifier",
        "in_list",
        "include",
    ]


def test_a_macro_completes_as_a_call_with_placeholders(assistant: _Assistant) -> None:
    source = "WHERE tpl.if_"
    assert assistant.complete(source, len(source)) == [
        Completion(
            "if_set",
            "macro",
            "tpl.if_set(:value, expr[, otherwise])",
            assistant.project.templates.macros["if_set"].doc,
            "if_set(:${1:value}, ${2:expr})",
        )
    ]


def test_an_include_completes_the_macro_templates(assistant: _Assistant) -> None:
    source = "FROM tpl.include('in"
    assert assistant.complete(source, len(source)) == [
        Completion("inner.tpl.sql", "template")
    ]


def test_a_parameter_completes_from_the_file(assistant: _Assistant) -> None:
    source = "WHERE a = :alpha AND b IN :beta AND c = :"
    labels = [one.label for one in assistant.complete(source, len(source))]
    assert labels == ["alpha", "beta"]
    assert assistant.complete("SELECT x::", 10) == []


def test_hover_shows_how_a_macro_is_called(assistant: _Assistant) -> None:
    source = "WHERE tpl.mine(:teams)"
    assert assistant.hover(source, 12) == (
        "```sql\ntpl.mine(:teams)\n```\n\nRows of any of the teams."
    )
    assert assistant.hover(source, 2) is None


def test_definition_goes_to_the_macro_and_to_the_included_file(
    assistant: _Assistant, project: Path
) -> None:
    assert assistant.definition("WHERE tpl.mine(:t)", 11) == Target(
        project / "lsp_macros.py", 5, 4
    )
    source = "FROM tpl.include('inner.tpl.sql') AS i"
    assert assistant.definition(source, 20) == Target(
        project / "sql" / "inner.tpl.sql", 0
    )


# positions


@pytest.mark.parametrize(
    ("source", "offset", "position"),
    [
        ("ab\ncd", 4, (1, 1)),
        ("имя\nx", 2, (0, 2)),
        ("😀x\ny", 1, (0, 2)),
        ("😀x\ny", 2, (0, 3)),
    ],
)
def test_positions_count_utf16_as_the_protocol_does(
    source: str, offset: int, position: tuple[int, int]
) -> None:
    assert position_of(source, offset) == position
    assert offset_of(source, *position) == offset


# the server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"  # pygls runs on asyncio


@pytest.mark.anyio
async def test_the_server_answers_an_editor(project: Path) -> None:
    from lsprotocol import types
    from pygls.lsp.client import LanguageClient

    client = LanguageClient("test", "1")
    published: asyncio.Future[types.PublishDiagnosticsParams] = (
        asyncio.get_running_loop().create_future()
    )

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def diagnostics(params: types.PublishDiagnosticsParams) -> None:
        if not published.done():
            published.set_result(params)

    await client.start_io(sys.executable, "-m", "sqlakit._cli", "lsp", cwd=str(project))
    await client.initialize_async(
        types.InitializeParams(
            capabilities=types.ClientCapabilities(), root_uri=project.as_uri()
        )
    )
    client.initialized(types.InitializedParams())
    uri = (project / "sql" / "new.tpl.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(uri, "sql", 1, "SELECT 1\nWHERE tpl.nope(:x)")
        )
    )
    found = await asyncio.wait_for(published, 10)
    [diagnostic] = found.diagnostics
    assert (diagnostic.range.start.line, diagnostic.range.start.character) == (1, 6)
    assert diagnostic.message.startswith("unknown macro tpl.nope")

    completion = await client.text_document_completion_async(
        types.CompletionParams(types.TextDocumentIdentifier(uri), types.Position(1, 10))
    )
    assert isinstance(completion, types.CompletionList)
    assert "mine" in [item.label for item in completion.items]

    text = "SELECT 1 WHERE tpl.mine(:t)"
    mine = (project / "sql" / "mine.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(types.TextDocumentItem(mine, "sql", 1, text))
    )
    at = types.Position(0, text.index("mine") + 1)
    here = types.TextDocumentIdentifier(mine)
    places = [
        await client.text_document_definition_async(types.DefinitionParams(here, at)),
        await client.text_document_implementation_async(
            types.ImplementationParams(here, at)
        ),
        await client.text_document_declaration_async(types.DeclarationParams(here, at)),
    ]
    assert [
        (place.uri, place.range.start.line, place.range.start.character)
        for place in places
        if isinstance(place, types.Location)
    ] == [((project / "lsp_macros.py").resolve().as_uri(), 5, 4)] * 3

    outer = (project / "sql" / "outer.tpl.sql").as_uri()
    client.text_document_did_open(
        types.DidOpenTextDocumentParams(
            types.TextDocumentItem(
                outer, "sql", 1, (project / "sql" / "outer.tpl.sql").read_text()
            )
        )
    )
    links = await client.text_document_document_link_async(
        types.DocumentLinkParams(types.TextDocumentIdentifier(outer))
    )
    assert [link.target for link in links or []] == [
        (project / "sql" / "inner.tpl.sql").resolve().as_uri()
    ]

    await client.shutdown_async(None)
    client.exit(None)
    await client.stop()


# macros written in SQL


def test_sql_macros_complete_and_hover_like_the_others(assistant: _Assistant) -> None:
    source = "WHERE tpl.for_"
    assert assistant.complete(source, len(source)) == [
        Completion(
            "for_team",
            "macro",
            "tpl.for_team(t)",
            "Rows of the team the call asks for.",
            "for_team(${1:t})",
        )
    ]
    assert assistant.hover("WHERE tpl.for_team(u)", 12) == (
        "```sql\ntpl.for_team(t)\n```\n\nRows of the team the call asks for."
    )


def test_an_sql_macro_is_defined_at_its_name(
    assistant: _Assistant, project: Path
) -> None:
    assert assistant.definition("WHERE tpl.visible(u)", 11) == Target(
        project / "_macros.sql", 3, 38
    )


def test_a_file_of_sql_macros_is_checked_as_it_stands(
    assistant: _Assistant, project: Path
) -> None:
    path = project / "_macros.sql"
    assert assistant.applies_to(path)
    assert assistant.diagnose(path, SQL_MACROS) == []
    broken = SQL_MACROS.replace("t.public", "tpl.nope(t)")
    [found] = assistant.diagnose(path, broken)
    assert broken[found.start : found.end] == (
        "SELECT tpl.for_team(t) OR tpl.nope(t) AS visible FROM t;"
    )
    assert found.message.startswith("Unknown macro tpl.nope in _macros.sql:4")


def test_a_template_calling_an_sql_macro_wrongly_is_marked(
    assistant: _Assistant, project: Path
) -> None:
    source = "SELECT * FROM users AS u WHERE tpl.visible(u, 1)"
    [found] = assistant.diagnose(project / "sql" / "new.sql", source)
    assert source[found.start : found.end] == "tpl.visible(u, 1)"
    assert found.message == "tpl.visible: takes 1 arguments, got 2"


def test_check_names_a_broken_sql_macro(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("inner.tpl.sql", "outer.tpl.sql", "open.sql"):
        (project / "sql" / name).unlink()
    (project / "_macros.sql").write_text(SQL_MACROS.replace("t.public", "tpl.nope(t)"))
    assert main(["check"]) == 1
    assert (
        capsys.readouterr()
        .out.splitlines()[0]
        .startswith("_macros.sql:4:1: Unknown macro tpl.nope in _macros.sql:4")
    )


# sqlakit export sqruff


def test_export_writes_what_sqruff_needs_into_pyproject(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "sql" / "dotted.sql").write_text(
        "SELECT * FROM users WHERE team = :c.team LIMIT :limit"
    )
    assert main(["export", "sqruff", "--check"]) == 1
    assert main(["export", "sqruff", "--dialect", "snowflake"]) == 0
    assert main(["export", "sqruff", "--check"]) == 0
    written = (project / "pyproject.toml").read_text()
    assert written == PYPROJECT + (
        "\n"
        "[tool.sqruff.core]\n"
        'dialect = "snowflake"\n'
        'templater = "placeholder"\n'
        'exclude_rules = "RF01,AL05,ST03"\n'
        "\n"
        "[tool.sqruff.templater.placeholder]\n"
        "# `sqlakit export sqruff` writes what the templates need, and keeps\n"
        "# what you add.\n"
        'param_style = "colon"\n'
        'limit = "1"\n'
    )
    assert not (project / ".sqruffignore").exists()
    assert main(["export", "sqruff"]) == 0
    assert (project / "pyproject.toml").read_text() == written
    assert capsys.readouterr().out.splitlines()[-1] == "wrote pyproject.toml"


def test_export_keeps_what_is_yours(project: Path) -> None:
    (project / "pyproject.toml").write_text(
        PYPROJECT
        + '\n[tool.sqruff.core]\ndialect = "postgres"\nrules = "core"\n'
        + '\n[tool.sqruff.templater.placeholder]\nparam_style = "colon"\nold = "1"\n'
        + "\n[tool.other]\nkept = true\n"
    )
    assert main(["export", "sqruff"]) == 0
    written = (project / "pyproject.toml").read_text()
    assert 'dialect = "postgres"\nrules = "core"\n' in written
    assert 'old = "1"' in written
    assert written.endswith('old = "1"\n\n[tool.other]\nkept = true\n')
    assert main(["export", "sqruff", "--check"]) == 0


def test_sqruff_reads_a_template_with_what_export_wrote(project: Path) -> None:
    import shutil
    import subprocess

    assert main(["export", "sqruff", "--dialect", "postgres"]) == 0
    sqruff = shutil.which("sqruff")
    assert sqruff is not None
    ran = subprocess.run(  # noqa: S603 - the linter the project installs
        [sqruff, "lint", "--parsing-errors", "sql/good.tpl.sql", "_macros.sql"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "Unparsable" not in ran.stdout + ran.stderr


# Python that names templates

CODE = """from app import User, db, other

print("имя"); db.sql("good.tpl.sql", teams=[])
db.sql.from_file("missing.sql")
User.query.from_sql("inner.tpl.sql")
db.sql.from_string("SELECT 1 -- not a file.sql")
other.sql("not_a_template")
"""


def test_the_code_is_checked_for_templates_that_are_not_there(
    assistant: _Assistant,
) -> None:
    [found] = assistant.python_diagnose(CODE)
    assert CODE[found.start : found.end] == "missing.sql"
    assert found.message == "No SQL template named `missing.sql` in sql."


def test_a_template_name_in_the_code_goes_to_its_file(
    assistant: _Assistant, project: Path
) -> None:
    offset = CODE.index("good.tpl.sql") + 3
    assert assistant.python_definition(CODE, offset) == Target(
        project / "sql" / "good.tpl.sql", 0
    )
    assert assistant.python_definition(CODE, CODE.index("print")) is None


def test_a_template_name_completes_in_the_code(assistant: _Assistant) -> None:
    source = 'rows = db.sql("go'
    assert assistant.python_complete(source, len(source)) == [
        Completion("good.tpl.sql", "template")
    ]
    assert assistant.python_complete("print('go", 9) == []


def test_template_names_are_links(assistant: _Assistant, project: Path) -> None:
    code = project / "code.py"
    assert [
        (CODE[start:end], target.name)
        for start, end, target in assistant.links(code, CODE)
    ] == [("good.tpl.sql", "good.tpl.sql"), ("inner.tpl.sql", "inner.tpl.sql")]
    outer = project / "sql" / "outer.tpl.sql"
    source = outer.read_text()
    [(start, end, target)] = assistant.links(outer, source)
    assert (source[start:end], target) == (
        "inner.tpl.sql",
        project / "sql" / "inner.tpl.sql",
    )


def test_the_server_reads_the_python_of_the_project(
    assistant: _Assistant, project: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    assert assistant.reads_python(project / "code.py")
    assert not assistant.reads_python(project / "tests" / "test_code.py")
    assert not assistant.reads_python(tmp_path_factory.mktemp("elsewhere") / "x.py")
    assert not assistant.reads_python(project / "sql" / "good.tpl.sql")


def test_a_file_of_sql_macros_passes_arguments_where_parameters_go(
    assistant: _Assistant, project: Path
) -> None:
    source = (
        "SELECT tpl.if_set(negate, col NOT IN (vals), col IN (vals)) AS picked\n"
        "FROM col, vals, negate;\n"
    )
    assert assistant.diagnose(project / "_macros.sql", source) == []


def test_check_passes_a_file_of_macros_among_the_templates(
    app: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (app / "shop" / "sql" / "_macros.sql").write_text(
        "SELECT tpl.if_set(negate, NOT col, col) AS flipped FROM col, negate;\n"
        "\n"
        "-- Rows of the team.\n"
        "SELECT t.team = :team AS for_team FROM t;\n"
    )
    assert main(["check", "--project", str(app)]) == 0
    assert capsys.readouterr().out == "1 templates, 0 problems\n"


def test_a_macro_over_several_lines_is_defined_at_its_alias(tmp_path: Path) -> None:
    path = tmp_path / "_macros.sql"
    path.write_text(
        "-- Several lines.\nSELECT tpl.if_set(\n    x, y\n) AS long_one\nFROM x, y;\n"
    )
    [macro] = sql_macros(path)
    assert _source_of(macro) == Target(path, 3, 5)


def test_a_column_goes_to_the_editor_in_utf16(tmp_path: Path) -> None:
    path = tmp_path / "_macros.sql"
    path.write_text("SELECT 'имя😀' = x AS named FROM x;\n")
    [macro] = sql_macros(path)
    target = _source_of(macro)
    assert target == Target(path, 0, 21)
    assert _utf16_column(target) == 22


def test_export_gives_a_stage_and_a_sample_the_value_a_linter_reads(
    project: Path,
) -> None:
    (project / "sql" / "copy.sql").write_text(
        "COPY INTO orders FROM :location;\n"
        "LIST :listed;\n"
        "COPY INTO :target\nFROM (SELECT * FROM t SAMPLE (:percent) LIMIT :limit)\n"
        "HEADER = TRUE;\n"
        "COPY INTO :table FROM @stage;\n"
    )
    assert load_project(project).placeholder_values() == {
        "limit": "1",
        "listed": "@stage/path",
        "location": "@stage/path",
        "percent": "10",
        "table": "table_",
        "target": "@stage/path",
    }


def test_a_file_macro_is_read_from_the_code_with_its_file(app: Path) -> None:
    (app / "shop" / "sql" / "tenant.sql").write_text(
        "SELECT t.tenant_id = :tenant_id AS for_tenant FROM t;\n"
    )
    (app / "shop" / "tenant.py").write_text(
        "from sqlakit.sql import Sql, sql_macro\n\n\n"
        '@sql_macro("sql/tenant.sql")\n'
        "def for_tenant(t: Sql) -> dict:\n"
        '    return {"tenant_id": 1}\n'
    )
    templates = load_project(app).templates
    macro = templates.macros["for_tenant"]
    assert getattr(macro, "sql_path", None) == app / "shop" / "sql" / "tenant.sql"
    assert templates.names() == ["users.sql"]
