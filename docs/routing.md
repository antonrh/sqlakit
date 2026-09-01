# Multiple databases

Configure the registry with one entry per database. `"default"` is the one
everything falls back to:

```python
from sqlakit import db

db.configure(
    {
        "default": {"url": PRIMARY_URL},
        "replica": {"url": REPLICA_URL},
        "warehouse": {"url": WAREHOUSE_URL},
    }
)

db.session  # the default one
db["replica"].session  # another, by alias
```

An entry accepts the same arguments as `Database`: a URL or the parts to build
one, plus its own `engine_args`, `session_args` and `templates`.

This page covers where a model lives, which database a block runs on, and how
to send a single query somewhere else.

## Where does a model live?

On the database set in its `__db__`, which a base class can set once for
everything under it:

```python
from sqlakit.orm import Model


class WarehouseBase(Model):
    __abstract__ = True
    __db__ = "warehouse"


class Report(WarehouseBase):
    __tablename__ = "reports"
```

Reads, writes and the tables created by
[`provisioned_tables()`](testing.md) all follow it.

`__db__` is part of the [model layer](models.md). With a plain mapped class,
pass the database directly: `warehouse.query(Report)`.

`__db__` requires a class you can edit. For models you cannot edit, such as
models from a third-party package, or when the placement belongs in settings,
use a **router**, which makes the same decision from outside:

```python
# app/db.py
from sqlakit import Router

from vendor.audit.models import AuditEntry, AuditFile

ELSEWHERE = {AuditEntry: "audit", AuditFile: "audit"}


class Placement(Router):
    def db_for(self, model: type) -> str | None:
        return ELSEWHERE.get(model)
```

```python
db.route(Placement())
db.route(lambda model: ELSEWHERE.get(model))  # a plain function also works
```

`configure()` also accepts routers, by import path, so the policy can live in
settings rather than in a call somewhere:

```python
db.configure(
    {
        "default": {"url": PRIMARY_URL},
        "audit": {"url": AUDIT_URL},
    },
    routers=["app.db.Placement"],
)
```

A router is a class with `db_for`, or any callable of the same shape:

```python
def db_for(model: type) -> str | None: ...
```

The only argument is the model class. Not the instance, not the statement, and
not whether this is a read or a write, so a router cannot split reads from
writes.

The return value is the alias the model lives on, or `None` if the router has
no answer for that model. The alias must be one the registry was configured
with. Otherwise resolving it raises `UnknownDatabaseError`.

Routers are called in the order given, and the first one that returns an alias
decides. If none of them does, the model's own `__db__` applies, and failing
that the default database. `route()` with no arguments clears them.

A router runs every time a model resolves its database, so avoid expensive work
inside it. The answer for a given model must also stay the same: once it starts
changing, writes go to one database and reads to another.

## Which database is this block on?

`db.using(alias)` returns that database, and while one of its blocks is open it
stands in for the default one:

```python
with db.using("replica").connect():
    report = build_report()  # models on the default database read the replica
```

That one line covers the read-only path of an application. A tenant on its own
shard works the same way:

```python
async with db.using(shard_of(tenant)).transaction():
    await move_the_tenant_in()
```

`using()` only affects models without a `__db__` of their own. A model that
sets one, such as the warehouse model above, keeps using its own database, and
still needs a block open on it. Naming a database is not opening one:

```python
with db.using("replica").transaction():
    Report(name="quarterly").save()  # MissingSessionError: the warehouse has no block
```

The redirection ends with the block, including when the block ends by raising.

You can also enter `db.using("replica")` on its own: it only redirects and
doesn't open anything. Use it when the block is opened somewhere else:

```python
with db.using("replica"):
    ...
```

## A registry of the model's own

Everything above uses `sqlakit.db`, the registry an application imports. A set
of models can have its own instead, filled with databases you built yourself:

```python
Base.register_db(Database(DB1_URL), alias="db1")
Base.register_db(Database(DB2_URL), alias="db2")

with Base.dbs.using("db2").transaction():
    User(name="ada").save()
```

The first call builds the registry, so nothing global is configured.
`Base.dbs` reaches it, and a model under `Base` registers into the same one.
Placement works as above: `__db__`, the routers, then the open `using()` block.

`register` does the same on a registry directly, for a shard that appears while
the application runs:

```python
db.register("shard-7", Database(SHARD_URL))
```

The alias has to be free, and cannot be `default`, which the registry itself
is.

## Which database is this query on?

```python
User.query.using("replica").order_by("name").page(limit=20)
User.query.using(warehouse).count()  # a `Database` object instead of an alias
```

One query goes to the database you named, regardless of the surrounding block.

With [SQL templates](sql.md), the database you call decides: `db.sql(...)`
runs on the default one, `db["warehouse"].sql(...)` on the warehouse.
Routers decide where a *model* lives, and a template is not a model, so no
router applies to a template.

## What none of this does

**It does not open connections for you.** Naming a database only chooses one. A
statement still runs inside a block, and using a database no block has opened
raises `MissingSessionError`. That keeps the choice readable: the statements
run on the connection opened by the block you are inside.

**It does not split reads from writes behind your back.** A framework that
sends every read to a replica breaks inside a transaction: the row you just
wrote is missing from the very next read, because that read runs on another
connection, and an uncommitted transaction is invisible from there. Here the
block decides, so no configuration can cause this bug. To spread reads over
several replicas, make the choice where you open connections:

```python
async with db.using(random.choice(REPLICAS)).connect():
    return await get_report(request)
```

Round-robin, by region, or by whatever rule fits your deployment.

**It does not police relationships.** `SQLAlchemy` does not support a
relationship between models on two databases, so there is nothing to permit or
forbid.

Next: [debugging](debugging.md) for a recording that shows which database ran
what, and [testing](testing.md) for a schema and a rollback on every database.
