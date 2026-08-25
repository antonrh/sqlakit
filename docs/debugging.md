# Debugging

An application that talks to a database well is one you can watch. `recording()`
is how: it says what ran, how long it took, and what ran more than once.

```python
with db.recording("GET /users") as record:
    build_report()

record.count  # 12
record.milliseconds  # 8.4
record.duplicates  # the ones that ran more than once, by the SQL they ran
record.slowest
print(record)  # the statements, numbered and timed
```

The listeners go on for the block and come off after it, so nothing is watching
a database nobody is looking at. Blocks nest, each recording what runs inside
it, and the statements of another task belong to that task. The block stays
`with` under asyncio too, because recording listens rather than runs.

Transaction control is not counted. `BEGIN` and `COMMIT` reach a cursor on some
drivers and not others, so counting them would make the same code mean different
numbers on SQLite and PostgreSQL.

## In a log

```python
import logging

logger = logging.getLogger(__name__)

with db.recording(f"{request.method} {request.url.path}", logger=logger):
    response = await call_next(request)
```

One line goes out when the block ends, at a level the numbers choose. INFO for a
block that did little. WARNING once there are more than five statements, the
slowest passes 100ms, or anything repeats at all. ERROR past twenty statements,
a slowest statement that reaches half a second, or more than five repeats. What
counts against the time is the slowest statement, not the block as a whole.

`logger=` calls `Recording.log`, and the thresholds are its `busy`, `slow` and
`repeated` arguments. To move them, or to fix the level yourself, leave
`logger=` out and write that line:

```python
with db.recording("GET /users") as record:
    build_report()

record.log(logger, busy=50, slow=200.0)  # ERROR later on count, sooner on time
record.log(logger, level=logging.INFO)  # or always INFO
```

The same numbers go out as fields for a log that is read by machine:

```python
{
    "queries": 12,
    "milliseconds": 8.4,
    "slowest_milliseconds": 3.1,
    "duplicated": 4,
    "databases": ("default",),
    "label": "GET /users",
}
```

## Every request, in development

An ASGI middleware that records each request takes a few lines. Wrap it in a
`settings.DEBUG` check so the profiling stays out of production:

```python
import logging

logger = logging.getLogger(__name__)


class QueryLog:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        label = f"{request.method} {request.url.path}"

        with db.recording(label, logger=logger):
            await self.app(scope, receive, send)


if settings.DEBUG:
    app.add_middleware(QueryLog)
```

The log then reads like the application working, one line per request, and the
ones that deserve attention say so:

```text
INFO     GET /health: 0 queries in 0.0ms
INFO     GET /users/42: 2 queries in 3.1ms
WARNING  GET /users: 14 queries in 41.7ms (12 repeated)
ERROR    POST /import: 240 queries in 1841.2ms (238 repeated, slowest 612.4ms)
```

Turn `stacks=True` on when a line like the last one appears, and the recording
will say which line of yours issued the repeated query.

## Reading the SQL

`echo=True` prints the block's statements when it ends. A script or a notebook
has no logger to write to, and this saves wiring one:

```python
with db.recording(echo=True), db.connect():
    build_report()
```

```sql
3 queries in 12.4ms (2 repeated)
   1    4.2ms
        SELECT users.id, users.name FROM users WHERE users.team = ?
```

`print(record)` gives one line per statement, which suits a block that ran forty
of them. When it comes down to one, `pretty` lays it out instead:

```python
print(record.pretty)  # every statement, over several lines each
print(record.slowest.pretty)  # just the one that took longest
```

```sql
   1    4.2ms
        SELECT users.id,
               users.name
        FROM users
        WHERE users.team = ?
        ORDER BY users.name
```

Laying it out needs `sqlakit[debug]`, which brings in `sqlparse`. Without it you
get the statement as it ran, on one line. Debugging output that raised would be
worse than plain.

If your application prints with [rich](https://rich.readthedocs.io), the SQL is
coloured as well: a recording and a statement both hand it something to render,
and nothing here imports `rich` to do it. `echo=True` and `record.echo()` colour
it the same way when `rich` is installed, and print plainly when it is not.

```python
from rich import print

print(record)
```

## Where a query came from

```python
with db.recording(stacks=True) as record:
    render(dashboard)

record.statements[0].stack  # the frames of your code that led to it
```

That turns "this ran 40 times" into a line number. The stack is collected with
`traceback.extract_stack()` on every statement, which costs enough to be off by
default. Turn it on for the run where you are chasing an N+1.

## More than one database

`db.recording()` on the registry covers every database it has, and each
statement says which one ran it:

```python
with db.recording() as record:
    move_the_reports()

record.databases  # ("default", "warehouse")
```

`db["warehouse"].recording()` is that one on its own.

## In a test

`assert_queries` is the same recorder with an assertion around it, and it sits
next to `recording()` on the database. The next page is about it:

```python
with db.assert_queries(2):
    render_dashboard()
```

Next: [testing](testing.md), where the same recorder becomes an assertion each
test can make.
