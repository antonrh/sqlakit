"""The PyCharm DDL that declares the `tpl.` macros as SQL functions.

PyCharm checks SQL against the schema of a data source, and `tpl.if_set` is a
function of a schema it has never seen. `ddl` declares the macros as functions
of that schema, for a DDL data source to read, and `dialects` says which
dialect the template directories are written in.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from ._project import RESERVED
from ._sql import INCLUDE, Macro

if TYPE_CHECKING:
    from pathlib import Path

    from ._project import Project

__all__ = ["DIALECTS", "ddl", "dialects"]

DIALECTS = {
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "snowflake": "Snowflake",
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "sqlite": "SQLite",
}
"""PyCharm's name for each dialect."""

VARIADIC = 10
"""How many arguments past its own a macro that takes any number is declared with,
on a database with no variadic functions."""


def ddl(project: Project, dialect: str) -> str:
    """Return a function of the namespace's schema for each macro, as DDL."""
    namespace = project.templates.namespace
    snowflake = dialect == "snowflake"
    lines = [
        "-- Written by `sqlakit export pycharm`: the macros, for PyCharm to resolve.",
        f"CREATE SCHEMA {namespace};",
        "",
    ]
    macros = sorted(project.templates.macros.values(), key=lambda macro: macro.name)
    for macro in macros:
        lines.extend(_declared(macro, namespace, snowflake=snowflake))
    include = _name(INCLUDE, namespace, snowflake=snowflake)
    lines.append(
        "-- The query of another template, in parentheses, where a table goes."
    )
    if snowflake:
        lines.append(
            f"CREATE FUNCTION {include}(path VARCHAR) RETURNS TABLE (value VARIANT) "
            "AS 'SELECT NULL';"
        )
    else:
        lines.append(
            f"CREATE FUNCTION {include}(path text) RETURNS SETOF record "
            "LANGUAGE sql AS $$ SELECT NULL $$;"
        )
    return "\n".join(lines) + "\n"


def _declared(macro: Macro, namespace: str, *, snowflake: bool) -> list[str]:
    name = _name(macro.name, namespace, snowflake=snowflake)
    summary = macro.doc.split("\n\n", 1)[0]
    doc = summary.replace("\n", " ").replace("'", "''")
    required = [slot.name for slot in macro.slots if slot.required]
    optional = [slot.name for slot in macro.slots if not slot.required]
    lines = [f"-- {line}" for line in summary.splitlines()]
    if snowflake:
        extra = (
            [f"{macro.variadic.name}_{index}" for index in range(1, VARIADIC + 1)]
            if macro.variadic
            else []
        )
        arities = range(len(required), len(required) + len(optional) + len(extra) + 1)
        every = [*required, *optional, *extra]
        for count in arities:
            arguments = ", ".join(f"{_argument(one)} VARIANT" for one in every[:count])
            comment = f" COMMENT = '{doc}'" if doc else ""
            lines.append(
                f"CREATE FUNCTION {name}({arguments}) RETURNS VARIANT{comment} AS 'NULL';"
            )
    else:
        arguments = [
            *(f"{_argument(one)} anyelement" for one in required),
            *(f"{_argument(one)} anyelement DEFAULT NULL" for one in optional),
        ]
        if macro.variadic is not None:
            arguments.append(f"VARIADIC {_argument(macro.variadic.name)} anyarray")
        written = ", ".join(arguments)
        lines.append(
            f"CREATE FUNCTION {name}({written}) RETURNS anyelement "
            "LANGUAGE sql AS $$ SELECT NULL $$;"
        )
        if doc:
            types = ", ".join(
                [
                    *(["anyelement"] * (len(required) + len(optional))),
                    *(["anyarray"] if macro.variadic else []),
                ]
            )
            lines.append(f"COMMENT ON FUNCTION {name}({types}) IS '{doc}';")
    lines.append("")
    return lines


def _name(name: str, namespace: str, *, snowflake: bool) -> str:
    """Return a function's name, quoted when SQL keeps the word for itself."""
    if name.lower() not in RESERVED:
        return f"{namespace}.{name}"
    return f'{namespace}."{name.upper() if snowflake else name.lower()}"'


def _argument(name: str) -> str:
    return f'"{name}"' if name.lower() in RESERVED else name


def dialects(root: Path, paths: list[Path], dialect: str, written: str | None) -> str:
    """Return `.idea/sqldialects.xml` with the template directories in ``dialect``.

    ``written`` is the file as it is, whose other entries stay.
    """
    project = (
        ET.fromstring(written)  # noqa: S314 - the project's own file
        if written
        else ET.Element("project", version="4")
    )
    mappings = project.find("component[@name='SqlDialectMappings']")
    if mappings is None:
        mappings = ET.SubElement(project, "component", name="SqlDialectMappings")
    urls = {_url(root, path) for path in paths}
    for entry in list(mappings):
        if entry.get("url") in urls:
            mappings.remove(entry)
    for url in sorted(urls):
        ET.SubElement(mappings, "file", url=url, dialect=DIALECTS[dialect])
    ET.indent(project, space="  ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + ET.tostring(project, encoding="unicode")
        + "\n"
    )


def _url(root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return f"file://{path.resolve().as_posix()}"
    return f"file://$PROJECT_DIR$/{relative}"
