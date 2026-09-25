"""SQL a linter reads: the templates the docs show, and what the macros write.

A template is SQL so that `sqruff` reads it with the `placeholder` templater.
These run it over every `sql` block of the pages about templates, and over what
each built-in macro writes, on the databases the macros have forms for.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import default

from sqlakit import _sql as sql_module
from sqlakit._project import _PARAMETER_IN_TEXT
from sqlakit.sql import Context

ROOT = Path(__file__).parent.parent.parent
PAGES = [ROOT / "README.md", ROOT / "docs" / "index.md", ROOT / "docs" / "sql.md"]
BLOCK = re.compile(r"^```sql\n(.*?)^```", re.MULTILINE | re.DOTALL)
DIALECTS = ["postgres", "snowflake"]
STATEMENT = re.compile(r"\s*(SELECT|WITH|INSERT|UPDATE|DELETE|--|/\*)", re.IGNORECASE)

SQRUFF = shutil.which("sqruff")
pytestmark = pytest.mark.skipif(SQRUFF is None, reason="sqruff is not installed")


def unparsable(sql: str, dialect: str, tmp_path: Path) -> str:
    """Return what `sqruff` could not parse in the SQL, or nothing."""
    values = {
        found.group(1): found.group(1) if found.group(2) else "1"
        for found in _PARAMETER_IN_TEXT.finditer(sql)
        if found.group(1)
    }
    (tmp_path / "pyproject.toml").write_text(
        "[tool.sqruff.core]\n"
        f'dialect = "{dialect}"\n'
        'templater = "placeholder"\n\n'
        "[tool.sqruff.templater.placeholder]\n"
        'param_style = "colon"\n'
        + "".join(f'{name} = "{value}"\n' for name, value in values.items())
    )
    (tmp_path / "q.sql").write_text(sql)
    ran = subprocess.run(  # noqa: S603 - the linter the project installs
        [str(SQRUFF), "lint", "--parsing-errors", "q.sql"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    output = ran.stdout + ran.stderr
    return output if "Unparsable" in output else ""


def blocks() -> list[Any]:
    found = []
    for page in PAGES:
        for index, block in enumerate(BLOCK.findall(page.read_text())):
            if re.search(r"^-- tpl\.\w+", block, re.MULTILINE):
                continue  # a file of SQL macros holds expressions, not statements
            if not STATEMENT.match(block):
                continue  # what a recording prints, not a template
            found.append(pytest.param(block, id=f"{page.name}-{index}"))
    return found


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("block", blocks())
def test_a_documented_template_parses(block: str, dialect: str, tmp_path: Path) -> None:
    assert unparsable(block, dialect, tmp_path) == ""


EVERY_MACRO = """
SELECT
    tpl.json_object('id', o.id) AS doc,
    tpl.array_agg(o.name, o.id) AS names,
    tpl.string_agg(o.name, ', ', o.id) AS joined,
    tpl.icollate(o.name) AS folded
FROM tpl.on_dialect(postgresql = orders, snowflake = facts.orders) AS o
CROSS JOIN tpl.values(:rows) AS v
WHERE
    tpl.if_set(:q, tpl.icontains(o.name, :q))
    AND tpl.unless_set(:archived, NOT o.archived)
    AND tpl.between(o.placed_at, :since, :until, '[)')
    AND tpl.in_list(o.status, :statuses, :exclude)
    AND tpl.array_contains(o.tags, :tag)
    AND tpl.arrays_overlap(o.tags, tpl.array(:tags, 'text'))
    AND tpl.array_contains_all(o.tags, tpl.array(:tags, 'text'))
    AND o.id IN (tpl.each(:ids))
    AND tpl.identifier(:column, o.id, o.name) IS NOT NULL
GROUP BY o.id
ORDER BY tpl.order_by(:sort, id, name, 'nulls_last')
LIMIT :limit OFFSET :offset
"""

VALUES = {
    "rows": [(1, "a"), (2, "b")],
    "q": "a",
    "archived": None,
    "since": 1,
    "until": 2,
    "statuses": ["a"],
    "exclude": True,
    "tag": "x",
    "tags": ["a", "b"],
    "ids": [1, 2],
    "column": "name",
    "sort": ["name.desc", "id"],
    "limit": 10,
    "offset": 0,
}


def snowflake() -> default.DefaultDialect:
    dialect = default.DefaultDialect()
    dialect.name = "snowflake"
    return dialect


@pytest.mark.parametrize(
    ("dialect", "linted"),
    [(postgresql.dialect(), "postgres"), (snowflake(), "snowflake")],
)
def test_what_the_macros_write_parses(
    dialect: default.DefaultDialect, linted: str, tmp_path: Path
) -> None:
    template = sql_module.MacroTemplate("x.sql", EVERY_MACRO, sql_module.registered([]))
    ctx = Context(dialect.name, dialect.identifier_preparer, VALUES)
    assert unparsable(template.render(ctx), linted, tmp_path) == ""
