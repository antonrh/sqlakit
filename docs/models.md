# Models

This layer is optional. It puts `save()`, `delete()` and a query on the model
itself, the `Active Record` way. The rest of the library doesn't depend on it,
and plain `SQLAlchemy` models work with everything else in these pages. If
your application keeps its saving in repositories or services, you can skip
this page.

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

A model finds the registry by itself, so there's no wiring to do: no custom
base, no extra line on every model. If you have more than one database, or a
`Database` instance of your own instead of the registry, see
[which database a model uses](#which-database-a-model-uses).

Instances work on the session of the open block, the same one `db.session`
returns. The block is required: without one there's no session, and every call
above raises `MissingSessionError` rather than opening a connection on its
own.

## Persistence

A new instance needs `save()`. Without it the block ends without writing it,
transaction or not. Appending one to a relationship this block loaded is the
exception: the instance goes in with the row it was added to.

```python
with db.transaction():
    User(name="ada").save()
```

A row this block read is in the session already, so both of these write it:

```python
with db.transaction():
    user = User.query.get_one(1)
    user.name = "grace"  # the commit writes it

    other = User.query.get_one(2)
    other.name = "hopper"
    other.save()  # written here instead
```

The statements are the same either way. `save()` decides when, which matters
for a query later in the same block: it sees the change if the change was
written first.

The one place the timing costs anything is a loop that changes rows, where it
is a write per row rather than one for all of them, eleven statements against
two over ten rows. On a row nothing changed `save()` sends nothing, so a loop
that only reads costs the same with it or without.

What happens at the end is the block's to decide. Inside `transaction()`,
`save()` flushes and the transaction commits. Inside `connect()` or
`autocommit()` there is no transaction to wait for, so `save()` commits:

```python
with db.connect():
    User(name="hopper").save()  # committed here
```

## Queries on a model

`Model.query` gives you the query layer straight from the class. It reads, and
it writes many rows at once:

```python
User.query.get(1)
User.query.where(User.is_active).order_by(User.name).page(limit=20)
User.query.where(User.team == "red").update({"team": "green"})
```

Everything it can build, page and write is covered in [queries](queries.md),
and the [examples](examples.md) include a whole application built on it.

## Custom query methods {#adding-methods-to-a-models-query}

A model can have a query class of its own instead of the built-in one. Inherit
from `Query` and attach your class with `as_descriptor()`:

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

From then on `User.query` builds a `UserQuery`, both at runtime and for the
type checker. A method that narrows the query returns it, so your methods
chain with the built-in ones in any order. A method that runs the query
returns its result:

```python
User.query.in_team("red").order_by(User.name).page(limit=20)
User.query.in_team("red").deactivate()  # how many rows it updated
```

Rules you want on every model go on a base class. Refer to the model class
through `self.model`:

```python
from typing import Any


class AppQuery(Query[Any]):
    def recent(self) -> Self:
        return self.order_by(self.model.created_at.desc())


class Base(Model):
    __abstract__ = True

    query = AppQuery.as_descriptor()
```

A model under such a base can still define a query of its own, and
`UserQuery(AppQuery["User"])` keeps both.

`as_descriptor()` returns the class it was called on, so `User.query.active()`
type checks without any annotations on your part. If a model adds no methods
but you still want typed rows, use `QueryDescriptor(Query["User"])`.

## Instances from an earlier block

An instance belongs to the session of the block that loaded it. When the block
ends, the session closes, and saving that instance in another block raises
`DetachedInstanceError`:

```python
with db.transaction():
    user = User.query.get_one(1)

with db.transaction():
    user.name = "ada"
    user.save()  # DetachedInstanceError
```

`merge()` reads the row again and returns an instance attached to the current
session:

```python
with db.transaction():
    user = user.merge()
    user.name = "ada"
    user.save()
```

The method is explicit for a reason. `merge()` attaches the object to the
current session, but whatever your instance holds overwrites the row it
reads, so changes that someone else committed in the meantime are silently
lost. The simplest way to stay safe is to load and save a model
inside one block.

## Fields from a request

`update()` sets the fields you give it and rejects names the model doesn't
have. A typo raises an error instead of creating an attribute nobody reads:

```python
user.update(payload.model_dump(exclude_unset=True)).save()
```

`None` counts as a value like any other, because sending `None` is exactly how
a request clears a nullable field. So the dict has to contain what was *sent*,
not what was non-empty. In `pydantic` that means `exclude_unset=True`.

## Relationships without a query

`set_loaded()` gives a relationship a value and marks it as loaded:

```python
campaign.set_loaded("esp", esp)  # the one this code just used
campaign.set_loaded("thumbnail", None)  # known to be empty
```

It helps when a relationship uses `lazy="raise"` but the data is already in
memory: the rows were selected in one go for a whole page, the row was created
in this very block, or the instance outlived the block that loaded it. Once
the session closes, loading a relationship is no longer possible.

The method describes what the database already contains. It doesn't change
anything. The value isn't written on save, doesn't mark the instance as
modified, and doesn't update the other side of the relationship. The
alternative is `refresh(attribute_names=["esp"])`, which queries the database
and therefore can't be wrong, but costs a query and needs an open session.

## Instance state

Four properties tell you where an instance stands with the session, and
`refresh()` reads the row again:

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

`refresh()` discards what the session remembers about the instance and reads
the row from the database. An assertion after `refresh()` checks the database
rather than the session. [Testing](testing.md) relies on exactly that.

## Soft deletes {#soft-deletes}

`SoftDeletes` marks a row as deleted instead of removing it:

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
note.delete(force=True)  # actually deletes the row
```

Reads don't see marked rows, `get()` included, so you can't reach a marked
row by key either. Two builders lift that filter, and neither touches the
model's own [`__query_filter__`](queries.md#soft-deletes):

```python
Note.query.with_deleted()  # marked rows as well
Note.query.only_deleted()  # marked rows only
```

Bulk deletion marks rows too, so both paths behave the same:

```python
Note.query.where(Note.is_draft).delete()  # marks them
Note.query.only_deleted().delete(force=True)  # removes the marked rows for good
```

Both paths set the column with the database's `now()`, and on databases that
have `timestamptz` the column uses that type. If you'd rather use a column of
your own, set `__soft_delete__ = "removed_at"` and declare the column
yourself, without the mixin.

A few things to know before you enable it:

- **A unique index still includes marked rows.** `UNIQUE(email)` won't accept
  a new row with the same address. On `PostgreSQL`, build the index with
  `WHERE deleted_at IS NULL`.
- **Cascades don't fire.** No `DELETE` reaches the database, so
  `cascade="all, delete-orphan"` and `ON DELETE CASCADE` do nothing. Mark the
  children yourself.
- **A bulk `update()` skips marked rows**, the same as reads do, and
  `with_deleted()` lifts that for writes as well. An instance in hand behaves
  differently: `save()` writes it, marked or not.

## Model imports

A model is added to the metadata when its module is imported, and not before.
If your application keeps models next to the features they belong to, you
have to import them all somewhere:

```python
from sqlakit import import_models

import_models("app")  # app/billing/models.py, app/users/models/*, ...
```

If you forget to import some of them, nothing fails loudly. Here's where the
breakage appears:

- `alembic revision --autogenerate` compares the metadata with the database,
  and a model nobody imported looks like a table to **drop**.
- [`provisioned_tables()`](testing.md) creates the tables the metadata
  contains, so a test run starts without those tables.
- `relationship("Team")` cannot find a class nobody has defined yet.

If all your models live in one module, you don't need any of this: importing
that module is enough.

## The model's database {#which-database-a-model-uses}

By default a model goes to the registry's `"default"` alias. You can name
another alias, or hand the model a database directly:

```python
from sqlakit.orm import Model


class Event(Model):
    __tablename__ = "events"
    __db__ = "warehouse"  # an alias in the registry
```

If you have a `Database` instance of your own, pass it through `set_db()`.
Calling it once on a base class is enough: it sets `__db__`, and every model
under the base inherits it.

```python
from sqlakit import Database
from sqlakit.orm import Model

warehouse = Database("postgresql+psycopg://localhost/warehouse")


class WarehouseBase(Model):
    __abstract__ = True


WarehouseBase.set_db(warehouse)


class Event(WarehouseBase):
    __tablename__ = "events"


class Shipment(WarehouseBase):  # the same database, nothing to configure
    __tablename__ = "shipments"
```

`Model.db` returns that database, both on the class and on an instance.

## A declarative base of your own

The `Model` that ships with the library is `ModelMixin` on a plain declarative
base. If you need a base with settings of your own, mix `ModelMixin` into it
and put the settings where `SQLAlchemy` reads them: `type_annotation_map` only
works on the class that starts the hierarchy.

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

You declare a model on such a base the way `SQLAlchemy`'s dataclass mapping
expects, and `save()`, `delete()` and `query` keep working:

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

Dataclass mapping puts a couple of requirements on the columns, and they come
from `SQLAlchemy`, not from this library:

- **`init=False` on a key the database fills.** Otherwise the generated
  `__init__` requires an `id` that doesn't exist yet.
- **Columns with a default come last**, like dataclass fields. Use
  `default_factory` for a fresh value per row. A plain `default=utcnow()`
  would stamp every row with the time of import.

With an async database, use `sqlakit.asyncio.orm.ModelMixin`.

## Async models

The same layer in `sqlakit.asyncio.orm`, awaited:

```python
user = await User(name="ada").save()
await User.query.get_one(user.id)
await user.refresh()
await user.delete()
```

`Model.db`, `set_db()` and the state properties stay synchronous, because none
of them touches the database.

Next: [queries](queries.md) for everything `Model.query` can build, or
[multiple databases](routing.md) for moving a model to another database.
