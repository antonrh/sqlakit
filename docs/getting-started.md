# Getting started

Connecting to a database, writing inside a transaction, and a test that rolls
back after itself.

## Install

```console
$ pip install sqlakit
```

## Connect

Create a `Database`. The object itself opens nothing and does not create
`app.db` yet. It only remembers the URL, and the engine and the connection
arrive the first time something reaches for the database.

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

Models stay plain `SQLAlchemy` classes. SQLAKit does not ask for a base of its
own.

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

The transaction commits when the block ends, and rolls back if the block
raises.

`db.session` takes the session from the current context. Threading it through
every function as an argument is no longer your job:

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

`rename` opens nothing and commits nothing. The block around it decides both.

## Write a test

One fixture creates the tables once for the whole run. Each test then runs
inside a transaction of its own, rolled back at the end, so what one test wrote
never reaches the next:

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

Run them with `python -m pytest`, which puts the project directory on the path
so `app` imports. The schema goes when the run ends, so the tests want neither a
database prepared in advance nor cleaning up in between.

## What you have now

- A database that any code below a block can reach, with no function taking a
  session.
- A transaction that commits at the end of the block and rolls back when it
  raises.
- A test that writes rows and leaves the database as it found it.

## Next

- [Context](context.md) for the blocks there are, what each one commits, and how
  they nest.
- [Models](models.md) if you want instances that save themselves. The layer is
  optional.
- [Blocks under asyncio](context.md#async) if you are here for `FastAPI` or
  `aiohttp`: the same thing from `sqlakit.asyncio`, with `await`.
- [Examples](examples.md) for whole programs: a `FastAPI` service, and
  `SQLModel` with and without the model layer.
