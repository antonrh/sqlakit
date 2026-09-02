"""The server behind `sqlakit debugserver`, and what sends to it.

A recording travels as one `POST /record`. The page holds a `GET /stream`
open and the server writes to it as the records arrive, so nothing polls.
"""

from __future__ import annotations

import atexit
import contextlib
import datetime
import decimal
import enum
import json
import pathlib
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ._recording import Recording

__all__ = [
    "DebugServer",
    "Records",
    "create_server",
    "flush_recordings",
    "page",
    "send_recording",
    "write_report",
]

HISTORY = 200
"""How many recordings the server remembers, newest last."""

TIMEOUT = 0.5
"""How long a send waits before it gives up on the server."""

WAITING = 1000
"""How many recordings queue up to be sent before the newest are dropped."""


class Records:
    """The recordings the server holds, and the pages watching for more."""

    def __init__(self, history: int = HISTORY) -> None:
        self._lock = threading.Lock()
        self._records: deque[dict[str, Any]] = deque(maxlen=history)
        self._watchers: list[queue.Queue[dict[str, Any]]] = []

    def add(self, record: dict[str, Any]) -> None:
        """Keep a recording, and hand it to every page that is watching."""
        with self._lock:
            self._records.append(record)
            watchers = list(self._watchers)
        for watcher in watchers:
            watcher.put(record)

    def all(self) -> list[dict[str, Any]]:
        """Every recording held, oldest first."""
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        """Forget them."""
        with self._lock:
            self._records.clear()

    def watch(self) -> queue.Queue[dict[str, Any]]:
        """Return a queue that every later recording arrives on."""
        watcher: queue.Queue[dict[str, Any]] = queue.Queue()
        with self._lock:
            self._watchers.append(watcher)
        return watcher

    def unwatch(self, watcher: queue.Queue[dict[str, Any]]) -> None:
        """Stop feeding a page that has gone."""
        with self._lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)


@dataclass(frozen=True, slots=True)
class DebugServer:
    """Where recordings go, and who is sending them.

    A server watches several applications at once, so a recording says which
    one it came from:

    ```python
    with db.recording(
        "GET /users",
        debugserver=DebugServer("localhost", 5555, app="web", tags=("api",)),
    ):
        list_users()
    ```

    A plain `("localhost", 5555)` works too, and the application is then the
    program that sent it.
    """

    host: str
    port: int
    app: str | None = None
    tags: tuple[str, ...] = ()

    @classmethod
    def of(cls, value: DebugServer | tuple[str, int]) -> DebugServer:
        """Return the server a caller named, however they named it."""
        if isinstance(value, tuple):
            host, port = value
            return cls(host, port)
        return value

    @property
    def sender(self) -> str:
        """The name the page groups this application under."""
        return self.app or pathlib.Path(sys.argv[0]).name or "python"


def as_payload(
    recording: Recording, *, app: str, tags: Sequence[str] = ()
) -> dict[str, Any]:
    """Return what travels: the recording, flattened to what a page shows."""
    return {
        "app": app,
        "tags": list(tags),
        "label": recording.label,
        "count": recording.count,
        "at": time.time() * 1000,
        "milliseconds": recording.milliseconds,
        "duplicates": sum(len(group) for group in recording.duplicates.values()),
        "statements": [
            {
                "sql": statement.sql,
                "parameters": _printable(statement.parameters),
                "milliseconds": statement.milliseconds,
                "database": statement.database,
                "stack": list(statement.stack),
            }
            for statement in recording.statements
        ],
    }


def send_recording(recording: Recording, to: DebugServer | tuple[str, int]) -> None:
    """Queue a recording for the server, and return without waiting on it.

    A thread of its own does the sending, so the block that recorded pays
    nothing for a server that is slow, or down, or not there at all.
    """
    server = DebugServer.of(to)
    body = json.dumps(
        as_payload(recording, app=server.sender, tags=server.tags)
    ).encode()
    _start()
    with contextlib.suppress(queue.Full):
        _waiting.put_nowait((server, body))


def flush_recordings(timeout: float = TIMEOUT) -> None:
    """Wait for the queued recordings to go out, and give up after `timeout`.

    Runs at exit, so the last recording of a short script still arrives.
    """
    deadline = time.monotonic() + timeout
    while _waiting.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.005)


_waiting: queue.Queue[tuple[DebugServer, bytes]] = queue.Queue(WAITING)
_sender: threading.Thread | None = None
_lock = threading.Lock()


def _start() -> None:
    """Start the thread that does the sending, once."""
    global _sender  # noqa: PLW0603
    with _lock:
        if _sender is None:
            _sender = threading.Thread(target=_run, name="sqlakit-debug", daemon=True)
            _sender.start()
            atexit.register(flush_recordings)


def _run() -> None:
    while True:
        server, body = _waiting.get()
        try:
            _post(server, body)
        finally:
            _waiting.task_done()


def _post(server: DebugServer, body: bytes) -> None:
    """Hand one recording over, and say nothing if the server is not there."""
    request = urllib.request.Request(
        f"http://{server.host}:{server.port}/record",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT):  # noqa: S310
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def _printable(parameters: Any) -> Any:  # noqa: ANN401, PLR0911 - a type each
    """Return the parameters as something JSON carries, and SQL would take.

    A value the page puts back into the statement has to read as the value:
    `repr` would send a timestamp as `datetime.datetime(1994, 1, 24, ...)`, and
    the statement would carry that, quoted, instead of the time.
    """
    if isinstance(parameters, (list, tuple)):
        return [_printable(one) for one in parameters]
    if isinstance(parameters, dict):
        return {str(name): _printable(value) for name, value in parameters.items()}
    if isinstance(parameters, (str, int, float, bool, type(None))):
        return parameters
    if isinstance(parameters, enum.Enum):
        return _printable(parameters.value)
    if isinstance(parameters, (datetime.datetime, datetime.date, datetime.time)):
        return parameters.isoformat()
    if isinstance(parameters, (uuid.UUID, decimal.Decimal, datetime.timedelta)):
        return str(parameters)
    if isinstance(parameters, (bytes, bytearray, memoryview)):
        return bytes(parameters).hex()
    return repr(parameters)


class _Handler(BaseHTTPRequestHandler):
    """One request: the page, the records it starts from, or a new record."""

    server_version = "sqlakit"
    records: Records

    def do_POST(self) -> None:
        if self.path != "/record":
            self._respond(404, b"", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            record = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, b"", "text/plain")
            return
        self.records.add(record)
        self._respond(204, b"", "text/plain")

    def do_DELETE(self) -> None:
        if self.path != "/records":
            self._respond(404, b"", "text/plain")
            return
        self.records.clear()
        self._respond(204, b"", "text/plain")

    def do_GET(self) -> None:
        if self.path == "/":
            self._respond(200, page(), "text/html; charset=utf-8")
        elif self.path == "/records":
            body = json.dumps(self.records.all()).encode()
            self._respond(200, body, "application/json")
        elif self.path == "/stream":
            self._stream()
        else:
            self._respond(404, b"", "text/plain")

    def _stream(self) -> None:
        """Hold the connection open and write every recording as it arrives."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        watcher = self.records.watch()
        try:
            while True:
                try:
                    record = watcher.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": still here\n\n")  # keeps proxies awake
                else:
                    self.wfile.write(f"data: {json.dumps(record)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            self.records.unwatch(watcher)

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401
        """Say nothing: the page is the output, not the terminal."""


@cache
def page() -> bytes:
    """Return the page, built into one file by `bun run dist` in `debugserver/`."""
    return (files("sqlakit") / "debugserver.html").read_bytes()


def write_report(
    path: str | pathlib.Path,
    records: Sequence[Mapping[str, Any]],
    *,
    about: str = "",
) -> pathlib.Path:
    """Write the page with these recordings inside it, and return where.

    The page a server hands out asks it for the recordings; this one carries
    them, so it opens from a file with no server anywhere.
    """
    # Escaped, because a `</script>` in the data would end the tag it travels in.
    held = json.dumps({"records": list(records), "about": about}).replace(
        "<", "\\u003c"
    )
    whole = page().decode()
    # Before the page's own script, which reads it as it loads.
    filled = whole.replace(
        "</head>", f"<script>window.SQLAKit = {held}</script>\n</head>", 1
    )
    if filled == whole:  # pragma: no cover - the page has a head
        message = "the page has no </head> to put the recordings before"
        raise RuntimeError(message)
    written = pathlib.Path(path)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(filled, encoding="utf-8")
    return written


def create_server(
    host: str, port: int, records: Records | None = None
) -> ThreadingHTTPServer:
    """Return a server for these recordings, not yet running."""
    handler = type("Handler", (_Handler,), {"records": records or Records()})
    return ThreadingHTTPServer((host, port), handler)
