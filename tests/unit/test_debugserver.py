"""The server behind `sqlakit debugserver`, and what travels to it."""

from __future__ import annotations

import datetime
import decimal
import enum
import json
import threading
import urllib.error
import urllib.request
import uuid
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa

from sqlakit import Database, EngineArgs
from sqlakit._debugserver import (
    DebugServer,
    Records,
    as_payload,
    create_server,
    flush_recordings,
    page,
    send_recording,
    write_report,
)
from sqlakit._recording import Recording, Statement

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator
    from http.server import ThreadingHTTPServer


@pytest.fixture
def db() -> Iterator[Database]:
    database = Database("sqlite://", engine_args=EngineArgs(poolclass=sa.StaticPool))
    with database.connect() as connection:
        connection.execute(sa.text("CREATE TABLE players (id INTEGER PRIMARY KEY)"))
    yield database
    database.dispose()


@pytest.fixture
def records() -> Records:
    return Records()


@pytest.fixture
def server(records: Records) -> Iterator[ThreadingHTTPServer]:
    running = create_server("localhost", 0, records)
    threading.Thread(target=running.serve_forever, daemon=True).start()
    yield running
    running.shutdown()
    running.server_close()


def where(server: ThreadingHTTPServer, path: str = "") -> str:
    return f"http://localhost:{server.server_address[1]}{path}"


def get(server: ThreadingHTTPServer, path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(where(server, path)) as answer:
            return answer.status, answer.read(), answer.headers["Content-Type"]
    except urllib.error.HTTPError as refused:
        with refused:
            return refused.status or 0, refused.read(), ""


def a_recording(label: str = "GET /users") -> Recording:
    return Recording(
        label=label,
        statements=[
            Statement(
                sql="SELECT 1",
                parameters=(),
                duration=0.001,
                dialect="sqlite",
            )
        ],
    )


class Plan(enum.Enum):
    FREE = "free"


def test_what_the_page_reads_of_a_recording() -> None:
    payload = as_payload(a_recording(), app="web", tags=["api"])

    assert payload["app"] == "web"
    assert payload["tags"] == ["api"]
    assert payload["label"] == "GET /users"
    assert payload["count"] == 1
    assert payload["milliseconds"] == pytest.approx(1.0)
    assert payload["statements"][0]["sql"] == "SELECT 1"
    assert payload["statements"][0]["dialect"] == "sqlite"


def test_a_value_travels_as_the_value_rather_than_as_its_repr() -> None:
    when = datetime.datetime(1994, 1, 24, 3, 39, tzinfo=datetime.UTC)
    recording = Recording(
        statements=[
            Statement(
                sql="INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                parameters={
                    "id": uuid.UUID("01a05e91-0fac-7121-a752-b4bd2c49535d"),
                    "at": when,
                    "owed": decimal.Decimal("15.50"),
                    "plan": Plan.FREE,
                    "blob": b"\xde\xad",
                },
                duration=0.0,
            )
        ]
    )

    assert as_payload(recording, app="web")["statements"][0]["parameters"] == {
        "id": "01a05e91-0fac-7121-a752-b4bd2c49535d",
        "at": "1994-01-24T03:39:00+00:00",
        "owed": "15.50",
        "plan": "free",
        "blob": "dead",
    }


def test_the_page_is_what_the_build_last_wrote() -> None:
    assert page().startswith(b"<!doctype html>")
    assert b"SQLAKit" in page()


def test_a_recording_reaches_a_server(
    server: ThreadingHTTPServer, records: Records
) -> None:
    send_recording(a_recording(), ("localhost", int(server.server_address[1])))
    flush_recordings(timeout=2.0)

    held = records.all()

    assert [one["label"] for one in held] == ["GET /users"]
    assert held[0]["app"]  # the program that sent it, when none was named


def test_a_server_that_is_not_there_is_not_the_application_s_problem() -> None:
    # Nothing listens on the port, and the block that recorded carries on.
    send_recording(a_recording(), ("localhost", 9))
    flush_recordings(timeout=0.2)


def test_what_the_server_answers(server: ThreadingHTTPServer, records: Records) -> None:
    status, body, kind = get(server, "/")

    assert status == 200
    assert kind.startswith("text/html")
    assert body == page()

    records.add({"label": "GET /users", "count": 1, "statements": []})
    status, body, kind = get(server, "/records")

    assert status == 200
    assert json.loads(body)[0]["label"] == "GET /users"
    assert get(server, "/nowhere")[0] == 404


def test_a_recording_can_be_posted_and_forgotten(
    server: ThreadingHTTPServer, records: Records
) -> None:
    body = json.dumps(as_payload(a_recording(), app="web")).encode()
    request = urllib.request.Request(
        where(server, "/record"),
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as answer:
        assert answer.status == 204

    assert len(records.all()) == 1

    forget = urllib.request.Request(where(server, "/records"), method="DELETE")
    with urllib.request.urlopen(forget) as answer:
        assert answer.status == 204

    assert records.all() == []


def test_a_page_watching_is_handed_what_arrives(records: Records) -> None:
    watcher = records.watch()
    records.add({"label": "GET /users"})

    assert watcher.get(timeout=1)["label"] == "GET /users"

    records.unwatch(watcher)
    records.add({"label": "POST /posts"})

    assert watcher.empty()


def test_the_oldest_recordings_are_forgotten() -> None:
    records = Records(history=2)
    for number in range(3):
        records.add({"label": str(number)})

    assert [one["label"] for one in records.all()] == ["1", "2"]


def test_a_report_is_one_file_with_the_recordings_in_it(tmp_path: pathlib.Path) -> None:
    written = write_report(
        tmp_path / "reports" / "run.html",
        [as_payload(a_recording(), app="web")],
        about="1 test, 1 query",
    )
    held = written.read_text()

    assert written.exists()  # the directory was made along the way
    assert "window.SQLAKit" in held
    assert "1 test, 1 query" in held
    assert held.index("window.SQLAKit") < held.index("</head>")


def test_a_statement_cannot_end_the_tag_it_travels_in(tmp_path: pathlib.Path) -> None:
    recording = Recording(
        statements=[Statement(sql="SELECT '</script>'", parameters=(), duration=0.0)]
    )
    held = write_report(
        tmp_path / "run.html", [as_payload(recording, app="web")]
    ).read_text()

    assert "</script>'" not in held.split("window.SQLAKit")[1].split("</script>")[0]


def test_a_debug_server_says_who_is_sending() -> None:
    named = DebugServer("localhost", 5555, app="web", tags=("api",))

    assert named.sender == "web"
    assert DebugServer.of(("localhost", 5555)) == DebugServer("localhost", 5555)
    assert DebugServer.of(named) is named
    assert DebugServer("localhost", 5555).sender  # the program that sent it


def test_a_recording_names_the_dialect_that_ran_each_statement(db: Database) -> None:
    with db.recording() as recording, db.connect() as connection:
        connection.execute(sa.text("SELECT 1"))

    assert [one.dialect for one in recording.statements] == ["sqlite"]

    payload: dict[str, Any] = as_payload(recording, app="web")

    assert payload["statements"][0]["dialect"] == "sqlite"
