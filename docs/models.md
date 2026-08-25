# Models

The layer is optional. It puts `save()`, `delete()` and a query on the model
itself, the Active Record way. The rest of the library does not need it, and
plain SQLAlchemy models work with everything else in these pages. If your
application keeps saving in repositories or services, skip this one.

```python
from sqlalchemy.orm import Mapped, mapped_column

from sqlakit import db
from sqlakit.orm import Model  # sqlakit.asyncio.orm under asyncio

db.configure("sqlite:///app.db")


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


with db.transaction():
    user = User(name="ada").save()
    User.query.get_one(user.id)
    user.delete()
```

A model finds the registry by itself, so the wiring ends there: no base of your
own, no line on every model. The section on
[which database a model uses](#which-database-a-model-uses) is for those with
more than one database, or with a `Database` of their own instead of the
registry.

Instances work on the session of the open block, the one `db.session` hands
out. The block is required: without one there is no session, and every call
above raises `MissingSessionError` rather than opening a connection for itself.

## Saving

Inside a transaction `save()` flushes, so what was written is visible to the
next queries while the block decides its fate. In a block with no transaction,
which means `connect()` or `autocommit()`, it commits for itself.

```python
with db.transaction():
    User(name="ada").save()  # flushed; the transaction commits it

with db.connect():
    User(name="grace").save()  # committed here, with no transaction to wait for
```

## Queries on a model

`Model.query` opens the query layer straight off the class. It reads, and it
writes many rows at once:

```python
User.query.get(1)
User.query.where(User.is_active).order_by(User.name).page(limit=20)
User.query.where(User.team == "red").update({"team": "green"})
```

What it builds, how it pages and how it writes is covered in
[queries](queries.md). In [examples](examples.md) a whole application is built
on it.

## Adding methods to a model's query {#adding-methods-to-a-models-query}

A model can name a query class of its own instead of the built-in one. Inherit
from `Query` and pass the class through `as_descriptor()`:

```python
from typing import Self

from sqlakit.orm import Model, Query


class UserQuery(Query["User"]):
    def in_team(self, team: str) -> Self:
        return self.where(User.team == team)

    def deactivate(self) -> int:
        return self.update({"is_active": False})


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    team: Mapped[str]
    is_active: Mapped[bool]

    query = UserQuery.as_descriptor()
```

From then on `User.query` builds a `UserQuery`, at runtime and for the type
checker. A method that narrows returns the query, so it chains with the built-in
ones in any order. A method that runs the query returns its result:

```python
User.query.in_team("red").order_by(User.name).page(limit=20)
User.query.in_team("red").deactivate()  # how many rows it updated
```

Rules that hold for every model go on a base class, written through
`self.model`:

```python
from typing import Any


class AppQuery(Query[Any]):
    def recent(self) -> Self:
        return self.order_by(self.model.created_at.desc())


class Base(Model):
    __abstract__ = True

    query = AppQuery.as_descriptor()
```

A model under such a base can still name a query of its own, and
`UserQuery(AppQuery["User"])` keeps both.

`as_descriptor()` returns the class it was called on, so `User.query.active()`
type checks without annotations of yours. A model that adds no methods but wants
typed rows is served by `QueryDescriptor(Query["User"])`.

## Instances from an earlier block

An instance belongs to the session of the block that loaded it. The block ends,
the session closes, and saving that instance somewhere else raises
`DetachedInstanceError`:

```python
with db.transaction():
    user = User.query.get_one(1)

with db.transaction():
    user.name = "ada"
    user.save()  # DetachedInstanceError
```

`merge()` reads the row again and returns an instance living in the current
session:

```python
with db.transaction():
    user = user.merge()
    user.name = "ada"
    user.save()
```

The method is explicit for a reason. `merge()` attaches the object to the
current session, but the row it read is overwritten by what your instance holds,
and anyone else's changes that reached the database in the meantime disappear
without a word. The simplest rule here is to load and save a model inside one
block.

## Fields from a request

`update()` sets the fields it is given and rejects a name the model does not
have. A typo raises rather than growing an attribute nobody reads:

```python
user.update(payload.model_dump(exclude_unset=True)).save()
```

`None` is a value here like any other, since clearing a nullable field is
exactly how a request does it. So the dict has to hold what was *sent*, not what
was non-empty. In pydantic that is `exclude_unset=True`.

## Relationships without a query

`set_loaded()` gives a relationship a value and marks it as loaded:

```python
campaign.set_loaded("esp", esp)  # the one this code just used
campaign.set_loaded("thumbnail", None)  # known to be empty
```

It helps when a relationship carries `lazy="raise"` while the data is already in
memory. The rows were selected in one go for a whole page, or the row was
created by this very block, or the instance outlived the block that loaded it.
Once the session closes, loading a relationship is no longer possible.

The method describes what the database already holds rather than changing it.
The value is not written on save, does not mark the instance modified, and does
not reach the other side of the relationship. The other way to fill a
relationship is `refresh(attribute_names=["esp"])`, which asks the database and
therefore cannot be wrong, but costs a query and wants an open session.

## Instance state

Four properties say where an instance stands with the session, and `refresh()`
reads the row again:

```python
with db.transaction():
    user = User(name="ada")
    user.is_persisted  # False, there is no row yet
    user.modified_fields  # {'name'}

    user.save()
    user.is_persisted  # True
    user.is_modified  # False, nothing pending

    user.name = "grace"
    user.modified_fields  # {'name'}

    user.delete()
    user.was_deleted  # True
```

`refresh()` drops what the session remembers about the instance and reads the
row from the database. That turns an assertion into a check of the database
rather than of the session, and [testing](testing.md) uses it for exactly that.

## Soft deletes {#soft-deletes}

`SoftDeletes` marks a row deleted rather than removing it:

```python
from sqlalchemy.orm import Mapped, mapped_column

from sqlakit.orm import Model, SoftDeletes


class Note(Model, SoftDeletes):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]
```

It adds a `deleted_at` column, and `delete()` sets it:

```python
note.delete()  # UPDATE notes SET deleted_at = now()
note.restore()  # and back again
note.delete(force=True)  # the row goes
```

Reads do not see marked rows, `get()` included, so such a row is unreachable by
key. Two builders lift that, and neither touches the model's own
[`__query_filter__`](queries.md#hiding-rows-for-good):

```python
Note.query.with_deleted()  # marked rows as well
Note.query.only_deleted()  # marked rows only
```

Bulk deletion marks rows too, so both paths agree:

```python
Note.query.where(Note.is_draft).delete()  # marks them
Note.query.only_deleted().delete(force=True)  # empties the bin
```

Both paths set the column with the database's `now()`, and where the database
has `timestamptz` that is the type it gets. A column of your own is named with
`__soft_delete__ = "removed_at"` and declared yourself, without the mixin.

Worth knowing before turning it on:

- **A unique index still holds the marked row.** `UNIQUE(email)` will not take a
  new row with the same address. On PostgreSQL build the index with
  `WHERE deleted_at IS NULL`.
- **Cascades do not fire.** No `DELETE` reaches the database, so
  `cascade="all, delete-orphan"` and `ON DELETE CASCADE` stay quiet. Mark the
  children yourself.
- **A bulk `update()` skips marked rows**, as reads do, and `with_deleted()`
  lifts that for writes as well. With an instance in hand it is different:
  `save()` writes it, marked or not.

## Importing every model

A model reaches the metadata when its module is imported, and not before. An
application that keeps models next to the feature they belong to has to import
them all somewhere:

```python
from sqlakit import import_models

import_models("app")  # app/billing/models.py, app/users/models/*, ...
```

Forget to import them and the breakage is quiet. Here is where it surfaces:

- `alembic revision --autogenerate` compares the metadata with the database, and
  a model nobody imported reads as a table to **drop**.
- [`provisioned_tables()`](testing.md) creates what the metadata holds, so a
  test run comes up without those tables.
- `relationship("Team")` cannot find a class nobody has defined yet.

An application whose models live in one module needs none of this, and importing
that module is enough.

## Which database a model uses {#which-database-a-model-uses}

By default a model goes to the registry's `"default"` alias. Name another alias,
or hand it a database directly:

```python
from sqlakit.orm import Model


class Event(Model):
    __tablename__ = "events"
    __db__ = "warehouse"  # an alias in the registry
```

An application with a `Database` of its own passes it through `set_db()`. Once
is enough, on a base class: it sets `__db__`, every model under it inherits
that, and there is nothing to repeat.

```python
from sqlakit import Database
from sqlakit.orm import Model

warehouse = Database("postgresql+psycopg://localhost/warehouse")


class WarehouseBase(Model):
    __abstract__ = True


WarehouseBase.set_db(warehouse)


class Event(WarehouseBase):
    __tablename__ = "events"


class Shipment(WarehouseBase):  # the same database, nothing to say
    __tablename__ = "shipments"
```

`Model.db` hands back that database, on the class and on an instance.

## A declarative base of your own

The `Model` that ships is `ModelMixin` on a plain base. When a base of yours
carries settings, mix the mixin into it and put the settings where SQLAlchemy
reads them: `type_annotation_map` counts only on the class that starts the
hierarchy.

```python
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, MappedAsDataclass

from sqlakit import db
from sqlakit.orm import ModelMixin


class Model(ModelMixin, MappedAsDataclass, DeclarativeBase):
    __db__ = db

    type_annotation_map = {
        datetime: sa.DateTime(timezone=True),
        uuid.UUID: sa.Uuid(as_uuid=True),
    }
```

A model on such a base is declared the way SQLAlchemy's dataclass mapping wants,
and `save()`, `delete()` and `query` stay where they were:

```python
from datetime import UTC, datetime

from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    name: Mapped[str]
    team: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default_factory=utcnow)


with db.transaction():
    user = User(name="ada").save()

print(user)  # User(id=1, name='ada', team='', created_at=datetime(...))
```

Dataclass mapping puts requirements on the columns, and they come from
SQLAlchemy rather than from this library:

- **`init=False` on a key the database fills.** Otherwise the generated
  `__init__` demands an `id` the row does not have yet.
- **Columns with a default come last**, as dataclass fields do. A fresh value
  per row comes from `default_factory`, where a plain `default=utcnow()` would
  stamp every row with the time of import.

With an async database, take `sqlakit.asyncio.orm.ModelMixin`.

## Models under asyncio

The same layer in `sqlakit.asyncio.orm`, awaited:

```python
user = await User(name="ada").save()
await User.query.get_one(user.id)
await user.refresh()
await user.delete()
```

`Model.db`, `set_db()` and the state properties stay synchronous, since none of
them reaches the database.

Next: [queries](queries.md) for what `Model.query` builds, or
[multiple databases](routing.md) for moving a model onto another one.
