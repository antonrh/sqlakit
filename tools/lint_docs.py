"""Lint the documentation: its Python, and the habits its prose falls into.

Ruff reads Python, not Markdown, so the fenced blocks are handed to it one at
a time on standard input. `--stdin-filename` makes a violation point back
at the page it came from.

Ruff runs twice over each block: formatting, and the annotation rules. The rest
of `ruff check` has no say here, because a block is an excerpt whose names are
undefined and whose imports are missing on purpose.

The prose checks are the mechanical half of the style: the turns of phrase that
crept in often enough to be worth a rule. The rest is judgement, and belongs to
review.
"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

DOCS = Path(__file__).parent.parent / "docs"
BLOCK = re.compile(
    r"^```python(?: title=\"[^\"]*\")?\n(.*?)^```", re.MULTILINE | re.DOTALL
)


def blocks(page: Path) -> list[tuple[int, str]]:
    """Return the Python blocks of a page, with the line each starts on."""
    text = page.read_text()
    return [
        (text[: found.start()].count("\n") + 2, found.group(1))
        for found in BLOCK.finditer(text)
    ]


def ruff(
    arguments: list[str], name: str, source: str
) -> subprocess.CompletedProcess[str]:
    """Run ruff over one block, told which page it came from."""
    return subprocess.run(  # noqa: S603
        ["ruff", *arguments, "--stdin-filename", name, "-"],  # noqa: S607
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )


def check(page: Path, line: int, source: str) -> str | None:
    """Return what ruff says about one block, if it says anything.

    A block that pulls a file in is not Python at all. The file it names is
    linted where it lives, and running ruff over the marker rewrites it into
    something the docs then fail to include.
    """
    if source.lstrip().startswith("--8<--"):
        return None

    name = f"{page.name}:{line}.py"
    where = f"{page.relative_to(DOCS.parent)}:{line}"

    formatting = ruff(["format", "--check"], name, source)
    if formatting.returncode != 0:
        return f"{where}  {formatting.stderr.strip() or 'would be reformatted'}"

    # The examples are the code readers are asked for, so they are annotated like
    # the code around them.
    annotations = ruff(
        ["check", "--isolated", "--select", "ANN", "--ignore", "ANN401", "--no-cache"],
        name,
        source,
    )
    if annotations.returncode != 0:
        said = next(
            (
                part
                for part in annotations.stdout.splitlines()
                if part.startswith("ANN")
            ),
            "would not type check",
        )
        return f"{where}  {said}"
    return None


WIDTH = 80

PROSE = (
    ("\u2014", "an em-dash; use a full stop, a comma or a colon"),
    (r"\b(simply|obviously|of course|merely)\b", "a filler word"),
    (
        r"\b(easy|easily|trivial|trivially|straightforward|painless)\b",
        "a word that tells the reader their trouble is small",
    ),
    (r"\b(is|are|was|were) what\b", "a cleft; say what does what"),
    (r"\b(we|our|us|let's)\b", "the first person; the docs speak of the library"),
    (r"(?<!!)!(?!!)", "an exclamation mark"),  # `!!!` opens an admonition
    (r"^!!! \w+$", "an admonition with no title of its own"),
    (";", "a semicolon; write two sentences"),
    (
        (
            r"\b(end(s|ed)? up|spin(s|ning)? up|kick(s|ed)? off|reach(es|ed)? out"
            r"|dive(s)? into|carr(y|ies|ied) over|hand(s|ed)? (back|over|out)"
            r"|pass(es|ed|ing)? around|come(s)? from|came from)\b"
        ),
        "a phrasal verb; use the single plain verb",
    ),
)


def prose(page: Path) -> list[str]:
    """Return what the prose of a page does that the style rules out.

    The rules are the mechanical half of the style: the habits that crept in
    often enough to be worth catching without reading.
    """
    text = page.read_text()
    text = re.sub(
        r"```.*?```",
        lambda found: "\n" * found.group(0).count("\n"),
        text,
        flags=re.DOTALL,
    )
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("|", ">")):
            continue
        where = f"{page.relative_to(DOCS.parent)}:{number}"
        # Inline code is quoted, not spoken, so the prose rules leave it alone.
        spoken = re.sub(r"`[^`]*`", "", line)
        # An image is markup rather than a sentence, and its `!` is not a shout.
        spoken = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", spoken)
        for pattern, said in PROSE:
            if re.search(pattern, spoken, re.IGNORECASE):
                found.append(f"{where}  {said}")
        if len(line) > WIDTH and "](http" not in line and not line.startswith("|"):
            found.append(f"{where}  {len(line)} columns; the prose wraps at {WIDTH}")
    return found


RIDDLE = (
    # `Whether` and `How long` open a plain answer, and a question is a
    # question. `What a page shows` is the opener that says nothing.
    r"^(What|Which|Where|Who|Whatever)\b(?![^.]*\?)",
    "a riddle opener; name the thing and say what it does",
)

CODE = (
    ("—", "an em-dash; use a full stop, a comma or a colon"),
    (r"(?i)\b(simply|obviously|of course|merely)\b", "a filler word"),
    (
        r"(?i)\b(easy|easily|trivial|trivially|straightforward|painless)\b",
        "a word that tells the reader their trouble is small",
    ),
    (r"(?i)\b(is|are|was|were) what\b", "a cleft; say what does what"),
    (
        r"(?i)\b(we|our|us|let's)\b",
        "the first person; the code speaks of the library",
    ),
    RIDDLE,
)
"""The rules a docstring and a comment answer to, as a page answers to its own."""

SOURCE = ("sqlakit", "tests", "tools")


def said(path: Path) -> Iterator[tuple[int, str]]:
    """Return the prose of a module: its docstrings, then its comments."""
    text = path.read_text()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        written = ast.get_docstring(node, clean=True)
        if written is None:
            continue
        # The docstring starts a line after the `def` for a function, and on
        # the first line for a module. Close enough to find it by.
        start = 1 if isinstance(node, ast.Module) else node.lineno
        for number, line in enumerate(written.splitlines()):
            yield start + number, line
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string.lstrip("# ")


def source(path: Path) -> list[str]:
    """Return what the prose in this module does that the style rules out."""
    found = []
    for number, line in said(path):
        spoken = re.sub(r"`[^`]*`|``[^`]*``", "", line).strip()
        if not spoken or spoken.startswith(("noqa", "type:", "ty:", "ruff:")):
            continue
        for pattern, why in CODE:
            if re.search(pattern, spoken):
                found.append(f"{path}:{number}  {why}")
    return found


def main() -> int:
    problems = [
        problem
        for page in sorted(DOCS.glob("*.md"))
        for line, source_ in blocks(page)
        if (problem := check(page, line, source_)) is not None
    ]
    problems += [
        problem for page in sorted(DOCS.glob("*.md")) for problem in prose(page)
    ]
    problems += [
        problem
        for where in SOURCE
        for path in sorted((DOCS.parent / where).rglob("*.py"))
        for problem in source(path)
    ]
    for problem in problems:
        print(problem)
    print(f"\n{len(problems)} places need attention")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
