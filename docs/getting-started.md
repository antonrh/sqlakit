# Getting started

This page walks you through the basics: connecting to a database, writing
inside a transaction, and writing a test that cleans up after itself.

## Install

```console
$ pip install sqlakit
```

## Connect

Create a `Database`. It doesn't connect to anything yet and doesn't create
`app.db`. It only stores the URL. `SQLAKit` creates the engine and the first
connection the first time you use the database.

```python
# app/db.py
from sqlakit import Database

db = Database("sqlite:///app.db")
```

Run a statement:

```python
import sqlalchemy as sa

from app.db import db

with db.connect() as conn:
    print(conn.scalar(sa.text("SELECT 1")))  # 1
```

## Define a table

Models are plain `SQLAlchemy` classes. You don't need a special base class:

```python
# app/models.py
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
```

Create the table once:

```python
from app.models import Base, User

with db.transaction() as conn:
    Base.metadata.create_all(conn)
```

## Write and read

```python
with db.transaction():
    db.session.add(User(name="ada"))

with db.connect():
    print(db.session.scalars(sa.select(User.name)).all())  # ['ada']
```

The transaction commits when the block ends, and rolls back if the block raises
an exception.

`db.session` returns the session of the current block, so you don't have to
pass it through every function as an argument:

```python
# app/users.py
import sqlalchemy as sa

from app.db import db
from app.models import User


def rename(user_id: int, name: str) -> None:
    db.session.get_one(User, user_id).name = name
```

```python
from app.users import rename

with db.transaction():
    rename(1, "grace")

with db.connect():
    print(db.session.scalars(sa.select(User.name)).all())  # ['grace']
```

Notice that `rename` doesn't open a connection and doesn't commit. The block
around it takes care of both.

## Write a test

One fixture creates the tables once for the whole run. Each test then runs
inside its own transaction, which rolls back at the end, so data written by
one test never leaks into the next:

```python
# tests/conftest.py
from collections.abc import Iterator

import pytest

from app.db import db
from app.models import Base


@pytest.fixture(scope="session", autouse=True)
def tables() -> Iterator[None]:
    with db.provisioned_tables(Base.metadata):
        yield


@pytest.fixture(autouse=True)
def transaction() -> Iterator[None]:
    with db.transaction(rollback=True):
        yield
```

```python
# tests/test_users.py
import sqlalchemy as sa

from app.db import db
from app.models import User
from app.users import rename


def test_rename() -> None:
    user = User(name="ada")
    db.session.add(user)
    db.session.flush()

    rename(user.id, "grace")

    assert db.session.scalar(sa.select(User.name)) == "grace"
```

Run the tests with `python -m pytest`. It puts the project directory on the
path, so the `app` package can be imported. `provisioned_tables()` drops the
schema when the run ends, so you don't need a prepared database and there's
nothing to clean up in between.

## What you have now

- A database that any code inside a block can use, without a session argument
  in every function.
- A transaction that commits at the end of the block and rolls back on an
  exception.
- A test that writes rows and leaves the database as it found it.

## Next

- [Context](context.md): the available blocks, what each one commits, and how
  they nest.
- [Models](models.md) if you want instances that save themselves. The layer is
  optional.
- [Blocks under asyncio](context.md#async) if you're here for `FastAPI` or
  `aiohttp`: the same blocks from `sqlakit.asyncio`, with `await`. Install
  `sqlakit[asyncio]` for those.
- [Examples](examples.md): complete programs, including a `FastAPI` service and
  `SQLModel` with and without the model layer.
