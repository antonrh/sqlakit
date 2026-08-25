"""The tutorial is run here, exactly as it is written, so it cannot go stale."""

import re
import sys
from pathlib import Path

import pytest

DOCS = Path(__file__).parent.parent.parent / "docs" / "getting-started.md"
BLOCK = re.compile(r"^```python\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _steps() -> tuple[dict[str, str], str]:
    """Return the page's files, and the rest of it as one script.

    A block whose first line names a file is that file; the others are what a
    reader types as they follow along, in the order the page gives them.
    """
    files: dict[str, str] = {}
    script: list[str] = []
    for found in BLOCK.finditer(DOCS.read_text()):
        source = found.group(1)
        first, _, rest = source.partition("\n")
        if first.startswith("# ") and first.endswith(".py"):
            files[first.removeprefix("# ")] = rest
        else:
            script.append(source)
    return files, "\n".join(script)


def test_the_tutorial_runs_as_written(pytester: pytest.Pytester) -> None:
    files, script = _steps()
    (pytester.path / "app").mkdir()
    (pytester.path / "app" / "__init__.py").write_text("")
    (pytester.path / "tests").mkdir()
    for name, source in files.items():
        (pytester.path / name).write_text(source)
    (pytester.path / "run.py").write_text(script)

    result = pytester.run(sys.executable, "run.py")

    # The output the page promises, printed by the code the page shows.
    result.stdout.fnmatch_lines(["1", "['ada']", "['grace']"])


def test_the_tutorials_test_passes(pytester: pytest.Pytester) -> None:
    """Only the files, with nothing run first: the schema is the conftest's job."""
    files, _ = _steps()
    (pytester.path / "app").mkdir()
    (pytester.path / "app" / "__init__.py").write_text("")
    (pytester.path / "tests").mkdir()
    for name, source in files.items():
        (pytester.path / name).write_text(source)

    # `python -m pytest`, as the page says: it puts the project on the path.
    result = pytester.run(
        sys.executable, "-m", "pytest", "tests", "-p", "no:cacheprovider"
    )

    result.assert_outcomes(passed=1)
