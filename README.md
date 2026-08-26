# SQLAKit

SQLAKit takes the routine out of working with `SQLAlchemy`. It manages sessions
and transactions for you, and adds pagination, SQL templates, Active Record, and
tooling to debug and test your queries. The synchronous and async APIs are built
the same way, and one decorator is all that ties it to a framework.

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

Both functions use the same session without either one receiving it as an
argument. The context is set by the `@db.transaction` decorator. Everything that
reached the session inside the block goes to the database on commit.

Until a block is open there is no session: `db.session` raises
`MissingSessionError` rather than connecting quietly. `db.connection` behaves
the same way and raises `MissingConnectionError`.

## Connections and transactions

The blocks work as context managers and as decorators:

```python
with db.connect():  # a connection, with no transaction of its own
    ...

with db.transaction():  # commits at the end, rolls back on an exception
    ...

with db.autocommit():  # AUTOCOMMIT, holding nothing open
    ...
```

## SQL templates

Templates are written in `Jinja`, so one file holds a simple query as readily as
a report with window functions or a recursive CTE. Values are not pasted into
the statement text: [jinja2sql](https://github.com/antonrh/jinja2sql) lifts each
one into a parameter, so `{{ name }}` becomes `:name__1`. It needs the
`sqlakit[sql]` extra.

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

`templates=` says which directory to look in, and `typed()` says what type the
rows come back as.

The value never reaches the statement text: the database gets `:since__1`, with
the value travelling separately. There are no strings to concatenate, and
nowhere for an injection to come from.

The file's name goes into the SQL as a comment, so a slow query log shows at
once which template is the slow one.

### From a string

```python
db.sql.from_string("SELECT count(*) FROM users").scalars().one()
```

The same templating, with no directory to configure.

## Query builder

There is a query builder of its own over `select()`, so `where`, `join` and
`order_by` work as they always do. On top of that it adds what `select` has not:
ordering by string, limit-offset and cursor pagination, reading in batches, and
bulk writes. It works with any mapped class, with nothing to inherit from:

```python
db.query(User).where(User.is_active).order_by(User.name).all()
```

### Ordering by a string

`order_by` takes a string of the form `field.direction`, such as the one that
arrived as a query parameter. The name is checked against the model before any
SQL is assembled, so a field that is not yours to order by never gets through.
Instead of a statement you get `UnknownOrderFieldError`, and the message lists
what this model can order by:

```python
db.query(User).order_by("created_at.desc")  # or "name", "name.asc.nulls_last"
```

### Limit-offset pagination

`page()` counts how many rows matched altogether, so a list can say "page 3 of
12":

```python
page = db.query(User).order_by("name").page(limit=20, offset=40)

page.items
page.total
page.has_next
```

### Cursor pagination

`cursor_page()` starts from the row a cursor points at, so it does not slow down
with depth. There is no total here, and there are cursors to the neighbouring
pages instead:

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

`set_db()` tells a model which database to work on. Put it on a base class and
every model under it inherits the binding. With the global `db` from the
section below there is nothing to bind: the model finds it by itself.

The layer is separate and optional. Everything above works on plain `SQLAlchemy`
models, and if saving lives in your repositories or services, `sqlakit.orm` need
never be imported.

## Testing

A test runs inside a transaction that is rolled back at the end, so the code
under test cannot commit around it. `assert_queries` pins down how many
statements a block costs:

```python
with db.transaction(rollback=True), db.assert_queries(2):
    render(dashboard)
```

## Debugging queries

`recording()` says what ran, how long it took, and what ran more than once:

```python
import logging

logger = logging.getLogger(__name__)

with db.recording("GET /users", logger=logger) as record:
    list_users()

record.count
record.milliseconds
record.duplicates
```

With `logger=` one line goes out at the end of the block, at a level the numbers
choose: the more statements and repeats, the louder.

With `echo=True` the block prints the statements laid out over lines, and marks
the repeats:

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

The N+1 is visible at a glance: one statement for the users and two identical
ones for the teams. Laying it out over lines comes from the `sqlakit[debug]`
extra, and where the project has `rich`, the output is coloured too.

## The registry

To avoid passing a `Database` from module to module, configure the registry
once at startup:

```python
# app/main.py
from sqlakit import db

db.configure("postgresql+psycopg://localhost/app")
```

Any module then takes it by import:

```python
# app/users.py
from sqlakit import db

from app.models import User


def list_users() -> list[User]:
    return db.query(User).order_by("name").all()
```

## More than one database

That same registry holds several databases. Describe them under aliases, and a
block picks the one it wants:

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

The async version is built the same way: the same classes, the same methods.
Only the import differs:

```python
from sqlakit.asyncio import Database

db = Database("postgresql+psycopg://localhost/app")

async with db.transaction():
    page = await db.query(User).order_by("name").page(limit=20)
```

The builder stays synchronous throughout: `where` and `order_by` run nothing, so
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

The `Database` here has to come from `sqlakit.asyncio`. Take the synchronous one
and the block closes before the handler starts, and the handler fails with
`MissingConnectionError`.

The lifespan needs only `dispose()`. There is nothing to open at startup, since
the engine is built on the first request.

## Documentation

[Getting started](docs/getting-started.md) builds a database, a model and a test
from an empty file. The rest is under [`docs/`](docs/):
[queries](docs/queries.md), [SQL templates](docs/sql.md),
[models](docs/models.md), [testing](docs/testing.md),
[debugging](docs/debugging.md), [multiple databases](docs/routing.md) and [the
reference](docs/reference.md). Whole programs live in [`examples/`](examples/),
each one run by the test suite.
