# Context

Your code opens a block. Everything called inside it reads the same connection
and the same session, so none of those functions has to be handed either one.

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

Underneath it is a `ContextVar`, and everything else follows from that. A task
started inside a block inherits the binding. Another thread does not see it. And
two different `Database` objects never share one binding between them.

Every block works as a context manager and as a decorator, with parentheses and
without:

| | for |
| --- | --- |
| [`connect()`](#connect) | reads, and code that decides for itself when to commit |
| [`transaction()`](#transaction) | work that has to land whole or not at all |
| [`autocommit()`](#autocommit) | read paths, and statements a transaction will not have |

With no block open there is nothing to read. Reaching for one raises instead of
opening a connection behind your back:

```python
db.connection  # MissingConnectionError
db.session  # MissingSessionError
```

A library that opened a connection on demand would hide the moment it comes out
of the pool and the moment it goes back.

## connect() {#connect}

Binds a connection for the block, or reuses the one already bound.

```python
with db.connect() as conn:
    conn.execute(sa.text("SELECT 1"))
```

No session is built up front. It waits for the first `db.session`, and only then
takes its place on the same connection:

```python
with db.connect():
    db.in_session()  # False, while nobody has asked
    db.session  # opened right here
    db.in_session()  # True
```

A block that works through `db.connection` alone therefore builds no session at
all.

**It commits nothing by itself.** The block hands out a connection, and seeing
the work through is yours:

```python
with db.connect():
    db.session.add(User(name="ada"))  # goes when the session goes

with db.connect():
    db.session.add(User(name="ada"))
    db.session.commit()  # kept
```

`save()` from the model layer and `create()` on a query know this: in a block
with no transaction they commit for themselves.

## transaction() {#transaction}

Opens a connection and a transaction, commits when the block ends, and rolls
back if the block raises.

```python
with db.transaction():
    db.session.add(User(name="ada"))  # the block commits this


@db.transaction
def import_users(rows: list[Row]) -> None: ...
```

`db.in_transaction()` says whether one is open.

### Nesting

A block inside another takes the connection already bound rather than opening a
second one:

```python
with db.transaction() as conn:
    with db.connect() as inner:
        assert inner is conn
```

A second connection would sit in a transaction of its own. It would not see the
outer block's writes, it would spend the pool twice over, and it could deadlock
on rows the outer one holds.

The outermost block owns the commit. The blocks under it mark up the code and
cost no statements at all, so wrapping a helper in `transaction()` costs
nothing. Each still opens a session of its own, so code you call cannot close or
roll back the session of the block above it.

### Letting a nested block fail on its own

Without a savepoint a nested block cannot undo just its own work, and its
failure takes the whole transaction with it. `savepoint=True` changes that:

```python
with db.transaction():
    db.query(Note).create(text="kept")

    with suppress(ValidationError), db.transaction(savepoint=True):
        db.query(Note).create(text="undone")
        raise ValidationError
```

It is off by default, since every savepoint costs a round trip and buys nothing
when the failure ends the whole transaction anyway. On `PostgreSQL` there is a
second price. Past 64 subtransactions the backend's `subxid` cache overflows and
reads start going to `pg_subtrans`, which slows the whole cluster rather than
the one transaction.

### Standing outside a transaction

`join_nested=False` works the other way. Put it on a block whose nested ones
have to stand apart, and those open connections of their own. They will not see
its writes, and their writes outlive its rollback.

```python
with db.transaction(join_nested=False):
    with db.transaction():
        db.query(Audit).create(text="the attempt was made")

    raise RuntimeError  # the audit row stays
```

Auditing is the case for it, where the record of an attempt has to outlive the
attempt failing. It costs a second connection from the pool and a second
transaction that can deadlock with the first, so the flag belongs on the blocks
that need it.

### Committing despite a known error

```python
@db.transaction(commit_on_error=ContentBlockedError)
def moderate(post_id: int) -> None:
    save_verdict(post_id, check(post_id))
    raise ContentBlockedError
```

The exception still goes up, and what was written before it stays.

### Retries

A retry re-runs the block, which a `with` statement cannot do. So
`transaction(retry_on=...)` works as a decorator only. A type checker flags the
`with` straight away, and at runtime it raises `RetryNotSupportedError`:

```python
def is_conflict(exc: BaseException) -> bool:
    return isinstance(exc, sa.exc.DBAPIError) and exc.orig.sqlstate in {
        "40001",
        "40P01",
    }


@db.transaction(retry_on=is_conflict, max_retries=3)
def transfer(from_id: int, to_id: int, amount: int) -> None: ...
```

Make the block safe to run twice. Only the block that owns the transaction
retries: called from inside another, it runs once, since the snapshot that
caused the conflict is fixed for the whole transaction.

`max_retries` counts the extra attempts, so `max_retries=3` runs the block at
most four times. Between attempts it waits `backoff(attempt)` seconds, counting
from zero, which by default is around 0.1, 0.2 and 0.4, each with jitter, so
that a hundred workers that collided once do not collide again together. A
function of your own suits a longer wait, and `lambda _: 0.0` suits a test.

### Throwing the work away

`rollback=True` rolls back instead of committing on the way out, and turns
`savepoint` on, so a nested block can still fail on its own:

```python
with db.transaction(rollback=True):
    db.query(User).create(name="ada")  # gone when the block ends
```

That is how a test runs against a real database and leaves it as it found it.
The [testing](testing.md) page is about that.

Every argument with its default is in the
[reference](reference.md#transaction-arguments).

## autocommit() {#autocommit}

`SQLAlchemy` opens a transaction around everything, an ordinary read included.
For one `SELECT`, `connect()` writes this to the log:

```sql
BEGIN (implicit)
SELECT 1
ROLLBACK
```

No transaction was wanted here, and the round trips on both sides went to waste.
`autocommit()` takes the connection in `AUTOCOMMIT`, where every statement
commits itself:

```python
@db.autocommit
def get_dashboard(user_id: int) -> Dashboard: ...
```

The log for that same `SELECT` reads:

```sql
BEGIN (implicit; DBAPI should not BEGIN due to autocommit mode)
SELECT 1
ROLLBACK using DBAPI connection.rollback(); set skip_autocommit_rollback to prevent fully
```

The lines around the statement are still there, but they are no longer SQL.
`SQLAlchemy` says in the parentheses that it hands the driver no `BEGIN`, and in
place of a `ROLLBACK` it calls the driver's `rollback()` as the connection goes
back to the pool. Dialects that can skip that call as well turn it off with one
argument:

```python
db = Database(url, engine_args={"skip_autocommit_rollback": True})
```

### Statements a transaction will not have

`VACUUM`, `CREATE DATABASE` and `CREATE INDEX CONCURRENTLY` cannot run inside a
transaction, which is where these belong:

```python
with db.autocommit() as conn:
    conn.execute(sa.text("VACUUM ANALYZE users"))
```

Call them outside any transaction. **Inside one the block joins it** and takes
the same connection, and the database refuses to run such a statement there. It
fails with the database's own error, such as
`cannot VACUUM from within a transaction`.

### What survives an exception

Every statement is committed already, so after an exception the database keeps
all the work that ran before it:

```python
with db.autocommit():
    db.session.add(User(name="ada"))
    db.session.flush()  # committed
    raise RuntimeError  # `ada` stays
```

That is the point of the block, and the same reason writes belong in
`transaction()`.

## Blocks under `asyncio` {#async}

`sqlakit.asyncio` repeats all of it. The blocks are awaited, reading the context
is not:

```python
async with db.transaction():
    await db.session.flush()

db.connection  # the same
db.session  # the same
db.in_transaction()  # the same
```

### Concurrent tasks

A task started inside a block inherits its context, so every coroutine under
`asyncio.gather()` lands on one session and one connection:

```python
async with db.transaction():
    await asyncio.gather(load_users(), load_teams())  # both on one session
```

A `SQLAlchemy` session is not built for that, and the treacherous part is that
reads may go through without a single complaint. Let two tasks overlap on a
write and you get `InvalidRequestError: Session is already flushing`. Give each
task a block of its own, and each takes its own connection:

```python
async def load_users() -> list[User]:
    async with db.connect():
        return await db.query(User).all()


await asyncio.gather(load_users(), load_teams())
```

### Background tasks

`FastAPI` runs `BackgroundTasks` after the response, when the handler's block
has already closed. Reaching for `db.session` from there raises
`MissingSessionError`:

```python
@app.post("/users")
@db.transaction
async def create_user(background: BackgroundTasks) -> UserResponse:
    background.add_task(notify)  # runs when the session is gone
    ...


async def notify() -> None:
    async with db.transaction():  # a background task needs a block of its own
        ...
```

Next: [queries](queries.md) for what runs inside these blocks.
