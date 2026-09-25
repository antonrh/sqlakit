"""The `sqruff` settings in `pyproject.toml` that read templates as SQL.

The `placeholder` templater writes each `:name` as `name`. Where that reads as
something else, `settings` gives the parameter a value; see
`Project.placeholder_values`.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import TYPE_CHECKING

from ._project import LINT_EXCLUDED

if TYPE_CHECKING:
    from ._project import Project

__all__ = ["settings", "stale"]

_HEADER = re.compile(r"^\[tool\.sqruff\.templater\.placeholder\]\s*$", re.MULTILINE)
_NEXT_TABLE = re.compile(r"^\[", re.MULTILINE)


def stale(project: Project, pyproject: str) -> list[str]:
    """Return the tables of `pyproject.toml` that no longer read the templates."""
    sqruff = tomllib.loads(pyproject).get("tool", {}).get("sqruff", {})
    written = sqruff.get("templater", {}).get("placeholder", {})
    values = project.placeholder_values()
    checked = (
        ("[tool.sqruff.core]", "core" in sqruff),
        (
            "[tool.sqruff.templater.placeholder]",
            all(written.get(name) == value for name, value in values.items()),
        ),
    )
    return [table for table, fresh in checked if not fresh]


def settings(project: Project, pyproject: str, dialect: str | None) -> str:
    """Return `pyproject.toml` with the settings that read the templates.

    The values the templates need are written again each time, and a value
    added to the table by hand stays. `[tool.sqruff.core]` is written only when
    it is missing, so the rules set there stay.
    """
    sqruff = tomllib.loads(pyproject).get("tool", {}).get("sqruff", {})
    written = dict(sqruff.get("templater", {}).get("placeholder", {}))
    written.pop("param_style", None)
    text = pyproject
    if "core" not in sqruff:
        chosen = dialect or project.dialect
        core = [
            "[tool.sqruff.core]",
            *([f'dialect = "{chosen}"'] if chosen else []),
            'templater = "placeholder"',
            f'exclude_rules = "{",".join(LINT_EXCLUDED)}"',
        ]
        text = text.rstrip("\n") + "\n\n" + "\n".join(core) + "\n"
    merged = dict(sorted({**written, **project.placeholder_values()}.items()))
    table = "\n".join(
        [
            "[tool.sqruff.templater.placeholder]",
            "# `sqlakit export sqruff` writes what the templates need, and keeps",
            "# what you add.",
            'param_style = "colon"',
            *(f"{name} = {_toml_value(value)}" for name, value in merged.items()),
        ]
    )
    if found := _HEADER.search(text):
        following = _NEXT_TABLE.search(text, found.end())
        end = following.start() if following else len(text)
        after = "\n\n" if following else "\n"
        return text[: found.start()] + table + after + text[end:]
    return text.rstrip("\n") + "\n\n" + table + "\n"


def _toml_value(value: object) -> str:
    """Return a value as TOML writes it: a string quoted, a flag in lower case."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(str(value))
