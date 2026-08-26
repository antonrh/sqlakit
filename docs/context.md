# Context

Your code opens a block. Everything called inside it uses the same connection
and the same session, so you don't have to pass either one around.

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

Under the hood, the context lives in a `ContextVar`. A task started inside a
block inherits the binding, another thread doesn't see it, and two different
`Database` objects never share one binding.

Every block works as a context manager and as a decorator, with or without
parentheses:

| | for |
| --- | --- |
| [`connect()`](#connect) | reads, and code that commits on its own |
| [`transaction()`](#transaction) | writes that must commit or roll back as one unit |
| [`autocommit()`](#autocommit) | read paths, and statements that cannot run inside a transaction |

Outside a block there's no connection and no session. Accessing them raises an
error instead of silently opening a connection:

```python
db.connection  # MissingConnectionError
db.session  # MissingSessionError
```

If connections were opened on demand, you couldn't tell when one comes out of
the pool and when it goes back.

## connect() {#connect}

Binds a connection for the block, or reuses the one already bound.

```python
with db.connect() as conn:
    conn.execute(sa.text("SELECT 1"))
```

No session is created up front. The first time you access `db.session`, one is
created on the same connection:

```python
with db.connect():
    db.in_session()  # False, no session yet
    db.session  # the session is created here
    db.in_session()  # True
```

So a block that only uses `db.connection` never creates a session.

**It commits nothing by itself.** The block only provides a connection;
committing is up to you:

```python
with db.connect():
    db.session.add(User(name="ada"))  # lost when the session closes

with db.connect():
    db.session.add(User(name="ada"))
    db.session.commit()  # kept
```

`save()` from the model layer and `create()` on a query are the exception: in
a block with no transaction they commit on their own.

## transaction() {#transaction}

Opens a connection and a transaction, commits when the block ends, and rolls
back if the block raises.

```python
with db.transaction():
    db.session.add(User(name="ada"))  # the block commits this


@db.transaction
def import_users(rows: list[Row]) -> None: ...
```

`db.in_transaction()` returns whether a transaction is open.

### Nesting

A nested block reuses the connection that is already bound instead of opening
a second one:

```python
with db.transaction() as conn:
    with db.connect() as inner:
        assert inner is conn
```

A second connection would run in its own transaction: it wouldn't see the
outer block's writes, it would take another connection from the pool, and it
could deadlock on rows the outer transaction holds.

The outermost block owns the commit. Nested blocks run no statements of their
own, so wrapping a helper in `transaction()` costs nothing. Each nested block
still opens its own session, so code you call can't close or roll back the
session of the block above it.

### Letting a nested block fail on its own

Without a savepoint a nested block cannot undo only its own work: if it fails,
the whole transaction rolls back. `savepoint=True` changes that:

```python
with db.transaction():
    db.query(Note).create(text="kept")

    with suppress(ValidationError), db.transaction(savepoint=True):
        db.query(Note).create(text="undone")
        raise ValidationError
```

It's off by default: every savepoint costs a round trip, and it doesn't help
when a failure should end the whole transaction anyway. On `PostgreSQL` there
is a second cost. Past 64 subtransactions the backend's `subxid` cache
overflows, reads start going to `pg_subtrans`, and that slows down the whole
cluster, not just the one transaction.

### Running outside the surrounding transaction

`join_nested=False` does the opposite. Set it on a block, and its nested
blocks open connections of their own instead of joining it. They don't see
its writes, and their writes survive its rollback.

```python
with db.transaction(join_nested=False):
    with db.transaction():
        db.query(Audit).create(text="the attempt was made")

    raise RuntimeError  # the audit row stays
```

The typical use is auditing, where the record of an attempt has to survive
even when the attempt fails. The cost is a second connection from the pool and
a second transaction that can deadlock with the first, so only set the flag on
the blocks that need it.

### Committing despite a known error

```python
@db.transaction(commit_on_error=ContentBlockedError)
def moderate(post_id: int) -> None:
    save_verdict(post_id, check(post_id))
    raise ContentBlockedError
```

The exception is still raised, and everything written before it is committed.

### Retries

A retry has to re-run the block, and a `with` statement cannot do that. So
`transaction(retry_on=...)` works only as a decorator. If you use it with
`with`, the type checker flags it right away, and at runtime it raises
`RetryNotSupportedError`:

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
retries. Called from inside another transaction it runs once, because the
snapshot that caused the conflict is fixed for the whole transaction.

`max_retries` counts the extra attempts, so `max_retries=3` runs the block at
most four times. Between attempts it waits `backoff(attempt)` seconds,
counting from zero. The default is roughly 0.1, 0.2 and 0.4 seconds, each with
jitter, so workers that collided once don't all retry at the same moment. If
you need longer waits, pass a function of your own; in tests, `lambda _: 0.0`
skips the waiting.

### Rolling back at the end

`rollback=True` rolls back at the end of the block instead of committing, and
turns `savepoint` on, so a nested block can still fail on its own:

```python
with db.transaction(rollback=True):
    db.query(User).create(name="ada")  # rolled back when the block ends
```

That's how a test runs against a real database and leaves it unchanged. The
[testing](testing.md) page builds on this.

All arguments and their defaults are listed in the
[reference](reference.md#transaction-arguments).

## autocommit() {#autocommit}

`SQLAlchemy` opens a transaction around everything, including plain reads. For
a single `SELECT`, `connect()` produces this log:

```sql
BEGIN (implicit)
SELECT 1
ROLLBACK
```

The transaction here is useless, and the `BEGIN` and `ROLLBACK` round trips
are wasted. `autocommit()` uses the connection in `AUTOCOMMIT` mode, where
every statement commits on its own:

```python
@db.autocommit
def get_dashboard(user_id: int) -> Dashboard: ...
```

The same `SELECT` now logs:

```sql
BEGIN (implicit; DBAPI should not BEGIN due to autocommit mode)
SELECT 1
ROLLBACK using DBAPI connection.rollback(); set skip_autocommit_rollback to prevent fully
```

The lines around the statement are still there, but no SQL is sent: the driver
gets no `BEGIN`, and instead of a `ROLLBACK` statement `SQLAlchemy` calls the
driver's `rollback()` as the connection goes back to the pool. On dialects
that can skip that call as well, turn it off with one argument:

```python
db = Database(url, engine_args={"skip_autocommit_rollback": True})
```

### Statements that can't run in a transaction

`VACUUM`, `CREATE DATABASE` and `CREATE INDEX CONCURRENTLY` cannot run inside
a transaction. Run them here:

```python
with db.autocommit() as conn:
    conn.execute(sa.text("VACUUM ANALYZE users"))
```

Call them outside any transaction. **Inside a transaction the block joins it**
and uses the same connection, so the statement fails with the database's own
error, such as `cannot VACUUM from within a transaction`.

### What survives an exception

Every statement is already committed, so after an exception the database keeps
everything that ran before it:

```python
with db.autocommit():
    db.session.add(User(name="ada"))
    db.session.flush()  # committed
    raise RuntimeError  # `ada` stays
```

That's the intended behavior, and the reason writes belong in
`transaction()`.

## Blocks under `asyncio` {#async}

`sqlakit.asyncio` has the same blocks. Opening a block is awaited, reading the
context is not:

```python
async with db.transaction():
    await db.session.flush()

db.connection  # the same
db.session  # the same
db.in_transaction()  # the same
```

### Concurrent tasks

A task started inside a block inherits its context, so every coroutine under
`asyncio.gather()` runs on the same session and the same connection:

```python
async with db.transaction():
    await asyncio.gather(load_users(), load_teams())  # both on one session
```

A `SQLAlchemy` session isn't designed for concurrent use, and the dangerous
part is that reads may work without any error. When two tasks overlap on a
write, you get `InvalidRequestError: Session is already flushing`. Give each
task a block of its own, and each gets its own connection:

```python
async def load_users() -> list[User]:
    async with db.connect():
        return await db.query(User).all()


await asyncio.gather(load_users(), load_teams())
```

### Background tasks

`FastAPI` runs `BackgroundTasks` after the response, when the handler's block
is already closed. Accessing `db.session` there raises `MissingSessionError`:

```python
@app.post("/users")
@db.transaction
async def create_user(background: BackgroundTasks) -> UserResponse:
    background.add_task(notify)  # runs after the session is closed
    ...


async def notify() -> None:
    async with db.transaction():  # a background task needs a block of its own
        ...
```

Next: [queries](queries.md) for what runs inside these blocks.
