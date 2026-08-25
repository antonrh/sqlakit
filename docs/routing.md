# Multiple databases

Configure the registry with one entry per database, and `"default"` is the one
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

An entry takes what `Database` takes: a URL or the arguments to build one, plus
`engine_args`, `session_args` and `templates` of its own.

What follows is where a model lives, which database a block runs on, and how to
send a single query somewhere else.

## Where does a model live?

On the database its `__db__` names, which a base class says once for everything
under it:

```python
from sqlakit.orm import Model


class WarehouseBase(Model):
    __abstract__ = True
    __db__ = "warehouse"


class Report(WarehouseBase):
    __tablename__ = "reports"
```

Reads, writes and the tables [`provisioned_tables()`](testing.md) creates all
follow it.

`__db__` belongs to the [model layer](models.md). A plain mapped class is handed
its database instead: `warehouse.query(Report)`.

`__db__` needs a class you can edit. For the ones you cannot edit, such as
models from a package you depend on, or for a placement that belongs in
settings, a **router** answers the same question from outside:

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
db.route(lambda model: ELSEWHERE.get(model))  # a function does as well
```

`configure()` takes them too, by import path, so the policy can come from
settings rather than from a call somewhere:

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

There is one argument, and it is the model class. Not the instance, not the
statement, not whether this is a read or a write, so splitting reads from writes
through a router is not something it can do.

What comes back is the alias the model lives on, or `None` when the router has
nothing to say about that model. The alias has to be one the registry was
configured with, or resolving it raises `UnknownDatabaseError`.

Routers are asked in the order given, and the first with an answer decides. When
none of them answers, the word goes to the model's own `__db__`, and failing
that to the default database. `route()` with nothing clears them.

A router runs every time a model resolves its database, so expensive work inside
one is worth avoiding. The answer for a given model also has to stay the same.
Once it starts changing, the writes go to one database and the reads to another.

## Which database is this block on?

`db.using(alias)` hands back that database, standing in for the default one
while a block of it is open:

```python
with db.using("replica").connect():
    report = build_report()  # models on the default database read the replica
```

The read-only path of an application is that one line. A tenant on its own shard
is the same line:

```python
async with db.using(shard_of(tenant)).transaction():
    await move_the_tenant_in()
```

`using()` stands in only for models that have no `__db__` of their own. A model
that names one, such as the warehouse model above, keeps going to its own
database. The redirection ends when the block does, including when it ends by
raising.

Entered on its own it redirects and opens nothing, for a block someone else
opens:

```python
with db.using("replica"):
    ...
```

## Which database is this query on?

```python
User.query.using("replica").order_by("name").page(limit=20)
User.query.using(warehouse).count()  # a database, rather than a name
```

One query, wherever you said, whatever the block around it is doing.

[SQL templates](sql.md) name their database the same way, by starting from it.
`db.sql(...)` runs on the default one, `db["warehouse"].sql(...)` on that one.
Routers say where a *model* lives, and a template is not a model, so nothing
routes it for you.

## What none of this does

**It does not open connections for you.** Naming a database chooses one. A
statement still runs in a block, and reaching a database nothing has opened
raises `MissingSessionError`. That keeps the choice readable: the statements run
on the connection opened by the block you are inside.

**It does not split reads from writes behind your back.** A framework that sends
every read to a replica breaks inside a transaction. The row you have just
written is missing from the very next read, because that read travels on another
connection, and an uncommitted transaction is invisible from there. Here the
block decides, so that bug cannot arrive by configuration. Spreading reads over
several replicas is the same line as before, with the choice made where
connections are opened:

```python
async with db.using(random.choice(REPLICAS)).connect():
    return await get_report(request)
```

by turn, by region, or by whatever your deployment knows.

**It does not police relationships.** A relationship between models on two
databases does not work in SQLAlchemy, so there is nothing to permit or forbid.

Next: [debugging](debugging.md) for a recording that says which database ran
what, and [testing](testing.md) for a schema and a rollback on every one.
