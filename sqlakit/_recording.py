from __future__ import annotations

import logging
import sys
import sysconfig
import traceback
import uuid
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from .exceptions import DEFAULT_ALIAS

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from os import PathLike
    from typing import TextIO

    import sqlparse

    from .types import QueryStats
else:
    try:
        import sqlparse
    except ImportError:  # pragma: no cover - the extra is installed in CI
        sqlparse = None

__all__ = ["Recording", "Statement"]


_LIBRARIES = (
    str(Path(__file__).parent),
    str(Path(sa.__file__ or "").parent),
)

_INSTALLED = tuple(
    {
        sysconfig.get_paths()[where]
        for where in ("stdlib", "purelib", "platlib", "scripts")
        if sysconfig.get_paths().get(where)
    }
)
"""Where the code you did not write lives: the runner, the loop, the script."""

_WALK = 100
"""How far back a stack is read before the search for your own frames gives up."""

KEEP = 3
"""How many of your frames a statement remembers."""

WIDE = 8
"""How many to read when some of them are about to be left out."""

_TRUNCATE = 120


@dataclass(frozen=True, slots=True)
class Statement:
    """One statement the database was asked to run."""

    sql: str
    parameters: Any
    duration: float
    """How long it took, in seconds."""

    database: str = DEFAULT_ALIAS
    """Which database ran it, for a recording that covers more than one."""

    dialect: str = ""
    """What ran it: `postgresql`, `mysql`, `sqlite`, as SQLAlchemy names them."""

    stack: tuple[str, ...] = ()
    """Where it came from, when the recording was asked for stacks."""

    @property
    def milliseconds(self) -> float:
        """How long it took, in the unit a log line wants."""
        return self.duration * 1000

    @property
    def pretty(self) -> str:
        """The SQL laid out over several lines, for reading rather than scanning.

        Laying it out needs `sqlakit[debug]`; without it this is the statement as it
        ran, on one line.
        """
        return _formatted(self.sql)

    def __rich__(self) -> Any:  # noqa: ANN401
        """Hand `rich` the SQL to colour, if the application prints with it.

        Nothing here imports `rich`; this runs only when `rich` is the one
        doing the printing.
        """
        from rich.syntax import Syntax  # noqa: PLC0415

        return Syntax(self.pretty, "sql", background_color="default", word_wrap=True)

    def __str__(self) -> str:
        return " ".join(self.sql.split())


@dataclass
class Recording:
    """The statements of a block, and what they add up to.

    `Database.recording()` hands this back:

    ```python
    with db.recording() as record:
        build_report()

    record.count, record.milliseconds, record.duplicates, record.slowest
    ```
    """

    label: str | None = None
    statements: list[Statement] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False, compare=False)
    """A unique id, so a recording several blocks sent is kept once."""

    @property
    def count(self) -> int:
        """How many statements ran."""
        return len(self.statements)

    @property
    def duration(self) -> float:
        """How long they took together, in seconds."""
        return sum(statement.duration for statement in self.statements)

    @property
    def milliseconds(self) -> float:
        """How long they took together, in the unit a log line wants."""
        return self.duration * 1000

    @property
    def slowest(self) -> Statement | None:
        """The one that took longest, if anything ran at all."""
        if not self.statements:
            return None
        return max(self.statements, key=lambda one: one.duration)

    @property
    def databases(self) -> tuple[str, ...]:
        """The databases that ran anything, in the order they first did."""
        return tuple(dict.fromkeys(one.database for one in self.statements))

    @property
    def duplicates(self) -> Mapping[str, list[Statement]]:
        """The statements that ran more than once, by the SQL they ran.

        Parameters are not part of the SQL, so the N+1 that fetches one row a
        hundred times is one entry with a hundred statements in it.
        """
        grouped: dict[str, list[Statement]] = {}
        for statement in self.statements:
            grouped.setdefault(f"{statement.database}: {statement}", []).append(
                statement
            )
        return {sql: group for sql, group in grouped.items() if len(group) > 1}

    def log(
        self,
        logger: logging.Logger,
        *,
        level: int | None = None,
        busy: int = 20,
        slow: float = 500.0,
        repeated: int = 5,
    ) -> None:
        """Write a summary, at a level the numbers choose unless you name one.

        Left to itself it says INFO for a block that did little, WARNING once
        anything repeats or a statement passes 100ms, and ERROR past ``busy``
        statements, ``slow`` milliseconds or ``repeated`` repeated statements:
        the shape of a log you can watch rather than read.

        Args:
            logger: Where the summary goes.
            level: A level of your own, which turns the thresholds off.
            busy: Statements past which the block logs at ERROR.
            slow: Milliseconds past which the block logs at ERROR.
            repeated: Repeated statements past which the block logs at ERROR.

        """
        if level is None:
            level = self._level(busy=busy, slow=slow, repeated=repeated)
        logger.log(level, self.summary(), extra=dict(self.stats()))

    def echo(self, *, file: TextIO | None = None) -> None:
        """Print the summary and the statements, for a block with no logger.

        ```python
        with db.recording(echo=True):
            build_report()
        ```

        Coloured and laid out where `rich` and `sqlakit[debug]` are installed, plain
        where they are not. A service wants `log` instead.
        """
        try:
            from rich.console import Console  # noqa: PLC0415
        except ImportError:
            print(self.summary(), file=file)
            print(self.pretty, file=file)
        else:
            console = Console(file=file)
            console.print(self.summary(), markup=False, highlight=False)
            console.print(self)

    def summary(self) -> str:
        """Return the one line a log gets."""
        head = f"{self.count} queries in {self.milliseconds:.1f}ms"
        if self.label:
            head = f"{self.label}: {head}"
        notes = []
        if self.duplicates:
            repeated = sum(len(group) for group in self.duplicates.values())
            notes.append(f"{repeated} repeated")
        slowest = self.slowest
        if slowest is not None and slowest.milliseconds >= 100:  # noqa: PLR2004
            notes.append(f"slowest {slowest.milliseconds:.1f}ms")
        return f"{head} ({', '.join(notes)})" if notes else head

    def stats(self) -> QueryStats:
        """Return what this adds up to, as the fields a structured log takes."""
        slowest = self.slowest
        return {
            "queries": self.count,
            "milliseconds": round(self.milliseconds, 2),
            "slowest_milliseconds": round(slowest.milliseconds, 2) if slowest else 0.0,
            "duplicated": sum(len(group) for group in self.duplicates.values()),
            "databases": self.databases,
            "label": self.label,
        }

    def __str__(self) -> str:
        """Return the statements, numbered, with the repeated ones marked."""
        if not self.statements:
            return "no queries"
        numbered = list(enumerate(self.statements, 1))
        repeats = self._repeats()
        several = len(self.databases) > 1
        lines = []
        for index, statement in numbered:
            sql = str(statement)
            if len(sql) > _TRUNCATE:
                sql = f"{sql[:_TRUNCATE]}…"
            where = f"{statement.database}  " if several else ""
            lines.append(f"  {index:>2}  {statement.milliseconds:5.1f}ms  {where}{sql}")
            others = repeats.get(id(statement))
            if others:
                where = ", ".join(str(number) for number in others)
                lines.append(
                    f"      {' ' * 8}↑ same as {where} ({len(others) + 1} times in all)"
                )
        return "\n".join(lines)

    @property
    def pretty(self) -> str:
        """The statements, numbered, each laid out over several lines.

        `print()` shows this when the one-line listing has run out of room.
        """
        if not self.statements:
            return "no queries"
        repeats = self._repeats()
        several = len(self.databases) > 1
        lines = []
        for index, statement in enumerate(self.statements, 1):
            where = f"  {statement.database}" if several else ""
            lines.append(
                f"  {index:>2}  {statement.milliseconds:5.1f}ms{where}"
                f"{_repeated(repeats.get(id(statement)))}"
            )
            lines.extend(f"        {line}" for line in statement.pretty.splitlines())
        return "\n".join(lines)

    def __rich__(self) -> Any:  # noqa: ANN401
        """Hand `rich` the same listing as `pretty`, for it to colour."""
        from rich.console import Group  # noqa: PLC0415
        from rich.padding import Padding  # noqa: PLC0415
        from rich.text import Text  # noqa: PLC0415

        if not self.statements:
            return Text("no queries")
        repeats = self._repeats()
        several = len(self.databases) > 1
        parts: list[Any] = []
        for index, statement in enumerate(self.statements, 1):
            where = f"  {statement.database}" if several else ""
            said = _repeated(repeats.get(id(statement)))
            parts.append(
                Text(
                    f"  {index:>2}  {statement.milliseconds:5.1f}ms{where}{said}",
                    "yellow" if said else "dim",
                )
            )
            parts.append(Padding(statement.__rich__(), (0, 0, 0, 6)))
        return Group(*parts)

    def _repeats(self) -> dict[int, list[int]]:
        """Return, for each repeated statement, where else the same SQL ran."""
        numbered = list(enumerate(self.statements, 1))
        return {
            id(statement): [
                index
                for index, other in numbered
                if other is not statement and other in group
            ]
            for group in self.duplicates.values()
            for statement in group
        }

    def _level(self, *, busy: int, slow: float, repeated: int) -> int:
        slowest = self.slowest
        milliseconds = slowest.milliseconds if slowest else 0.0
        duplicated = sum(len(group) for group in self.duplicates.values())
        if self.count > busy or milliseconds >= slow or duplicated > repeated:
            return logging.ERROR
        if self.count > busy // 4 or milliseconds >= 100 or duplicated:  # noqa: PLR2004
            return logging.WARNING
        return logging.INFO


def _repeated(others: list[int] | None) -> str:
    """Return what to say about a statement that ran more than once."""
    if not others:
        return ""
    where = ", ".join(str(number) for number in others)
    return f"  ↑ same as {where} ({len(others) + 1} times in all)"


def require_expectation(
    count: int | None,
    at_most: int | None,
    duplicates: bool,  # noqa: FBT001  (it mirrors the caller's keyword)
) -> None:
    """Refuse a block that asserts nothing, before it runs rather than after.

    Args:
        count: The statements the block is expected to run.
        at_most: A ceiling on them.
        duplicates: Whether a statement may run more than once.

    Raises:
        TypeError: if none of the three was asked for.

    """
    if count is None and at_most is None and duplicates:
        message = "assert_queries needs something to assert"
        raise TypeError(message)


def check(
    recording: Recording,
    *,
    count: int | None,
    at_most: int | None,
    duplicates: bool,
) -> None:
    """Fail unless the recording matches what the block said.

    Args:
        recording: What the block ran.
        count: The statements it was expected to run.
        at_most: A ceiling on them.
        duplicates: Whether a statement may run more than once.

    Raises:
        AssertionError: with the reason and the statements behind it.

    """
    if count is not None and recording.count != count:
        _fail(recording, f"{recording.count} queries, expected {count}")
    if at_most is not None and recording.count > at_most:
        _fail(recording, f"{recording.count} queries, expected at most {at_most}")
    if not duplicates and recording.duplicates:
        repeated = sum(len(group) for group in recording.duplicates.values())
        _fail(recording, f"{repeated} of the queries repeat another")


def _fail(recording: Recording, reason: str) -> None:
    """Raise with the reason, and the statements that led to it under it."""
    message = f"{reason}\n\n{recording}\n"
    raise AssertionError(message)


def _formatted(sql: str) -> str:
    """Return the SQL laid out, or as it is when nothing can lay it out."""
    if sqlparse is None:  # pragma: no cover - the extra is installed in CI
        return " ".join(sql.split())
    return sqlparse.format(sql, reindent=True, keyword_case="upper").strip()


def resolved(paths: Sequence[str | PathLike[str]]) -> tuple[str, ...]:
    """Return these paths as they are on disk, for comparing with a frame."""
    return tuple(str(Path(one).resolve()) for one in paths)


def caller_stack(skip: Sequence[str] = (), keep: int = KEEP) -> tuple[str, ...]:
    """Return the frames of your own code that led to a statement.

    This library's and SQLAlchemy's are left out by directory rather than by
    name: a project may well live in a path carrying the library's name. So is
    generated code, `<string>`: SQLAlchemy builds wrappers that way, and the
    line numbers lead nowhere.

    Installed packages go too, the test runner and the event loop among them,
    which leaves the lines you wrote. A caller with none of its own, a library
    calling from `site-packages`, gets them back rather than nothing.

    ``skip`` names more to leave out, a file or a directory, so that the frames
    point past a factory of yours at whoever called it.
    """
    skipped = (*_LIBRARIES, *resolved(skip))
    frames = list(islice(_frames(), _WALK))
    return _yours(frames, (*skipped, *_INSTALLED), keep) or _yours(
        frames, skipped, keep
    )


def _yours(
    frames: Sequence[traceback.FrameSummary], skipped: tuple[str, ...], keep: int
) -> tuple[str, ...]:
    """Return the first few frames that none of these directories hold."""
    kept = []
    for frame in frames:
        if frame.filename.startswith(skipped) or frame.filename.startswith("<"):
            continue
        kept.append(f"{frame.filename}:{frame.lineno} in {frame.name}")
        if len(kept) == keep:
            break
    return tuple(kept)


def _frames() -> Iterator[traceback.FrameSummary]:
    """Every frame that led here, innermost first.

    An `asyncio` driver runs the statement in a greenlet of its own, and the
    application's frames are on the greenlet waiting for it: without those,
    an async project's statements come from nowhere.
    """
    yield from reversed(traceback.extract_stack()[:-1])
    for frame in _waiting():
        yield from reversed(traceback.extract_stack(frame))


def _waiting() -> Iterator[Any]:
    """Return the frame each greenlet above this one waits at, nearest first."""
    greenlet = sys.modules.get("greenlet")  # imported by SQLAlchemy, for asyncio
    if greenlet is None:
        return
    current = greenlet.getcurrent()
    while (current := getattr(current, "parent", None)) is not None:
        frame = getattr(current, "gr_frame", None)
        if frame is not None:
            yield frame
