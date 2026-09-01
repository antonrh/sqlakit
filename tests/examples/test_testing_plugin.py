"""`examples/testing_plugin`, run as its own session."""

from pathlib import Path

import pytest

EXAMPLE = Path(__file__).parent.parent.parent / "examples" / "testing_plugin"


@pytest.fixture
def project(pytester: pytest.Pytester) -> pytest.Pytester:
    for name in ("app.py", "conftest.py", "pytest.ini", "test_users.py"):
        (pytester.path / name).write_text((EXAMPLE / name).read_text())
    return pytester


def test_the_example_passes_as_it_stands(project: pytest.Pytester) -> None:
    project.runpytest_subprocess().assert_outcomes(passed=3)


def test_without_the_opt_in_the_marked_tests_have_no_database(
    project: pytest.Pytester,
) -> None:
    (project.path / "pytest.ini").write_text("[pytest]\n")

    result = project.runpytest_subprocess()

    result.assert_outcomes(passed=1, failed=2)
    result.stdout.fnmatch_lines(["*MissingSessionError*"])
