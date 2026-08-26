# SQLAKit

SQLAKit removes the boilerplate from `SQLAlchemy` applications. It manages
sessions and transactions for you, and adds a query builder with pagination
built in, `SQL` templates, an optional `Active Record` layer, debugging and
testing tools, etc. It supports both sync and async APIs and integrates
easily with any framework.

```console
$ pip install sqlakit
```

## A quick example

```python
import sqlalchemy as sa

from sqlakit import Database

from app.models import User

db = Database("postgresql+psycopg://localhost/app")


def get_user(email: str) -> User | None:
    return db.session.scalars(sa.select(User).where(User.email == email)).first()


@db.transaction
def get_or_create_user(email: str, name: str) -> User:
    user = get_user(email)
    if user is None:
        user = User(email=email, name=name)
        db.session.add(user)
    return user
```

Both functions use the same session without passing it around. The
`@db.transaction` decorator opens it, and commits when the function returns.

Outside a block there is no session: `db.session` raises `MissingSessionError`
instead of silently opening a connection. `db.connection` works the same way
and raises `MissingConnectionError`.

## Connections and transactions

All blocks work as context managers and as decorators:

```python
with db.connect():  # a connection, with no transaction of its own
    ...

with db.transaction():  # commits at the end, rolls back on an exception
    ...

with db.autocommit():  # AUTOCOMMIT, no transaction held open
    ...
```

## SQL templates

Templates are `Jinja` files, so they can hold anything from a one-line query to
a report with window functions or a recursive CTE.
[jinja2sql](https://github.com/antonrh/jinja2sql) turns every `{{ name }}` into
a bound parameter (`:name__1`), so values never end up in the SQL text and
there is no way to inject anything. Requires the `sqlakit[sql]` extra.

### From a file

```sql
-- reports/by_team.sql
SELECT team, count(*) AS members
FROM users
WHERE joined_at > {{ since }}
GROUP BY team
```

```python
from pydantic import BaseModel

from sqlakit import Database

db = Database(DATABASE_URL, templates=BASE_DIR / "sql")


class TeamReport(BaseModel):
    team: str
    members: int


db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
# [TeamReport(team='red', members=2)]
```

`templates=` sets the directory to load templates from, and `typed()` sets the
type each row is returned as.

The template name is added to the SQL as a comment, so a slow query log shows
right away which file a query came from.

### From a string

```python
db.sql.from_string("SELECT count(*) FROM users").scalars().one()
```

The same templating, with no directory to configure.

## Query builder

The query builder wraps `select()`, so `where`, `join` and `order_by` work as
usual. On top of that it adds what `select` lacks: ordering by string,
limit-offset and cursor pagination, reading in batches, and bulk writes. It
works with any mapped class, with nothing to inherit from:

```python
db.query(User).where(User.is_active).order_by(User.name).all()
```

### Ordering by a string

`order_by` accepts a `field.direction` string, for example straight from a
query parameter. The field name is checked against the model before any SQL is
built, so an unknown field never reaches the database. Instead you get
`UnknownOrderFieldError`, and its message lists the fields the model allows:

```python
db.query(User).order_by("created_at.desc")  # or "name", "name.asc.nulls_last"
```

### Limit-offset pagination

`page()` also counts the total, so you can show "page 3 of 12":

```python
page = db.query(User).order_by("name").page(limit=20, offset=40)

page.items
page.total
page.has_next
```

### Cursor pagination

`cursor_page()` continues from a cursor, so it stays fast at any depth. There
is no total; instead you get cursors to the next and previous pages:

```python
feed = db.query(User).order_by("created_at.desc").cursor_page(limit=20)

feed.items
feed.next_cursor
feed.previous_cursor
```

## Active Record

An instance saves and deletes itself, and the query is available on the class:

```python
from sqlalchemy.orm import Mapped, mapped_column

from sqlakit import Database
from sqlakit.orm import Model

db = Database("postgresql+psycopg://localhost/app")


class Note(Model):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]


Note.set_db(db)

with db.transaction():
    note = Note(text="ada")
    note.save()

    Note.query.where(Note.text == "ada").all()
    note.delete()
```

`set_db()` binds a model to a database. Call it on a base class and every model
under it inherits the binding. With the global `db` from the section below you
don't need it at all: the model uses the global registry automatically.

This layer is optional. Everything else works on plain `SQLAlchemy` models, so
if saving belongs in your repositories or services, skip `sqlakit.orm`
entirely.

## Testing

A test runs inside a transaction that is rolled back at the end, so nothing
the code under test writes is actually committed. `assert_queries` checks how many statements a
block runs:

```python
with db.transaction(rollback=True), db.assert_queries(2):
    render(dashboard)
```

## Debugging queries

`recording()` shows what ran, how long it took, and what ran more than once:

```python
import logging

logger = logging.getLogger(__name__)

with db.recording("GET /users", logger=logger) as record:
    list_users()

record.count
record.milliseconds
record.duplicates
```

With `logger=` one line is logged at the end of the block. The log level
depends on the numbers: more statements and more repeats mean a higher level.

With `echo=True` the block prints each statement, formatted and with repeats
marked:

```python
with db.recording(echo=True):
    list_users()
```

```sql
3 queries in 0.0ms (2 repeated)
   1    0.0ms
      SELECT users.team_id
      FROM users
      ORDER BY users.name ASC
   2    0.0ms  ↑ same as 3 (2 times in all)
      SELECT teams.id AS teams_id,
             teams.name AS teams_name
      FROM teams
      WHERE teams.id = ?
   3    0.0ms  ↑ same as 2 (2 times in all)
      SELECT teams.id AS teams_id,
             teams.name AS teams_name
      FROM teams
      WHERE teams.id = ?
```

The N+1 is easy to spot: one query for the users and two identical ones for
the teams. Formatting needs the `sqlakit[debug]` extra, and if the project has
`rich`, the output is colored too.

## The registry

To avoid passing a `Database` from module to module, configure the registry
once at startup:

```python
# app/main.py
from sqlakit import db

db.configure("postgresql+psycopg://localhost/app")
```

Any other module just imports it:

```python
# app/users.py
from sqlakit import db

from app.models import User


def list_users() -> list[User]:
    return db.query(User).order_by("name").all()
```

## More than one database

The registry can hold several databases. Configure them under aliases, and pick
one per block:

```python
from sqlakit import db

db.configure(
    {
        "default": {"url": PRIMARY_URL},
        "replica": {"url": REPLICA_URL},
    }
)

with db.using("replica").connect():
    list_users()  # the models read the replica
```

## The async API

The async API is identical: the same classes, the same methods. Only the import
changes:

```python
from sqlakit.asyncio import Database

db = Database("postgresql+psycopg://localhost/app")

async with db.transaction():
    page = await db.query(User).order_by("name").page(limit=20)
```

The builder itself stays synchronous: `where` and `order_by` run no SQL, so
there is nothing to await.

## `FastAPI` integration

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from app.models import User
from sqlakit.asyncio import Database

db = Database("postgresql+psycopg://localhost/app")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await db.dispose()  # close the pool on shutdown


app = FastAPI(lifespan=lifespan)


class UserCreate(BaseModel):
    name: str
    team: str = ""


class UserResponse(BaseModel, from_attributes=True):
    id: int
    name: str
    team: str


@app.post("/users", status_code=201)
@db.transaction  # one transaction, committed when the handler returns
async def create_user(payload: UserCreate) -> UserResponse:
    user = User(name=payload.name, team=payload.team)
    db.session.add(user)
    await db.session.flush()  # INSERT now, the id is needed for the response
    return UserResponse.model_validate(user)
```

No `Depends(get_session)`, no session factories, and no `async with` in the
handler.

Use the `Database` from `sqlakit.asyncio` here. With the sync one the block
closes before the async handler runs, and the handler fails with
`MissingConnectionError`.

There is nothing to open at startup: the engine is created on first use. On
shutdown, `dispose()` closes the pool.

## Where to go next

| | |
| --- | --- |
| [Getting started](getting-started.md) | a database, a model and a test from an empty file |
| [The database](databases.md) | building one, an instance or the registry, engine arguments |
| [Context](context.md) | the blocks, what each commits, how they nest |
| [Queries](queries.md) | the builder, ordering, limit-offset and cursor pagination, reading in batches |
| [SQL templates](sql.md) | statements in files and strings, values bound, rows typed |
| [Models](models.md) | the Active Record way: `save()`, `delete()`, soft deletes |
| [Testing](testing.md) | a schema once per run, a rollback around each test |
| [Debugging](debugging.md) | what ran, how long it took, what repeated |
| [Multiple databases](routing.md) | replicas, a warehouse, a shard per tenant |
| [Reference](reference.md) | every class, and the mapping for [asyncio](reference.md#async) |

Plain `SQLAlchemy` models work with all of it, `SQLModel` included. Complete
example apps are in the [examples](examples.md), and each one is run by the
test suite.
