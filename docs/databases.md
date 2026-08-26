# The database

A `Database` holds the `SQLAlchemy` engine, its connection pool, and the
context the blocks bind into. Creating it doesn't connect to anything yet: the
engine is created when the first block needs it.

```python
from sqlakit import Database

db = Database("postgresql+psycopg://localhost/app")
```

## A URL, or separate arguments

Settings usually come in pieces rather than as one string. Pass them as they
are, without assembling a URL by hand:

```python
db = Database(
    drivername="postgresql+psycopg",
    host=settings.DB_HOST,
    port=settings.DB_PORT,
    username=settings.DB_USER,
    password=settings.DB_PASSWORD,
    database=settings.DB_NAME,
)
```

The main reason to prefer this is the password. Passed as its own argument, it
is quoted automatically and comes back exactly as it went in. A password with
special characters inside a URL string raises no error: `SQLAlchemy` splits the
string on the first `@`, and parts of the password end up in the wrong fields:

```python
import sqlalchemy as sa

url = sa.make_url("postgresql+psycopg://u:p@ss/word@h/app")

url.password  # 'p'
url.host  # 'ss'
url.database  # 'word@h/app'
```

Pass either a complete URL or the arguments separately. If you pass both, you
get `ConflictingDatabaseUrlError`.

## An instance, or the registry

A database can be an instance of your own or an entry in the registry. The
pages here use both.

**Your own instance**, passed where it is needed:

```python
# app/db.py
from sqlakit import Database

db = Database(settings.DATABASE_URL)
```

Any module that needs it imports it from there, and a test can create a second
instance without touching the first.

**The registry**, configured once at startup:

```python
# app/main.py
from sqlakit import db

db.configure(settings.DATABASE_URL)
```

Any module then does `from sqlakit import db` and gets the same connections.
Reconfiguring is allowed until something connects; after that it raises
`DatabaseAlreadyConfiguredError`, and you have to call `db.dispose()` first.

`configure()` accepts everything the constructor accepts, separate arguments
included:

```python
# app/main.py
from sqlakit import db

db.configure(
    drivername="postgresql+psycopg",
    host=settings.DB_HOST,
    port=settings.DB_PORT,
    username=settings.DB_USER,
    password=settings.DB_PASSWORD,
    database=settings.DB_NAME,
)
```

The same registry can hold more than one database: see
[multiple databases](routing.md).

## Engine and session arguments

`engine_args` and `session_args` go straight to `create_engine` and
`sessionmaker`, merged over the defaults this library sets:

```python
db = Database(
    settings.DATABASE_URL,
    engine_args={
        "pool_size": 20,
        "max_overflow": 10,
        "echo": settings.DEBUG,
    },
    session_args={
        "autoflush": False,
    },
)
```

With the registry, pass the same arguments to `configure()`:

```python
db.configure(
    settings.DATABASE_URL,
    engine_args={
        "pool_size": 20,
        "max_overflow": 10,
    },
    templates=BASE_DIR / "sql",
)
```

Your arguments take precedence. The defaults, and the reasoning behind each,
are in the [reference](reference.md#defaults). `pool_size`, `max_overflow` and
`isolation_level` are not set by default: they depend on the worker count, the
database's limits and the dialect.

`templates=` sets the directory [SQL templates](sql.md) are loaded from.

## Starting and stopping

You never connect manually. Nothing opens until code inside a block uses the
database.

```python
db.ping()  # whether the database answers, for a health endpoint
db.dispose()  # close every connection the pool holds, on shutdown
```

Call `dispose()` where your framework shuts down; for `FastAPI` that is the
lifespan, and [examples](examples.md) shows it in place. A script that needs a
database for a single run can use `Database` as a context manager, which
disposes the engine at the end:

```python
with Database(url) as db, db.transaction():
    backfill()
```

Next: [context](context.md) for the blocks, or
[multiple databases](routing.md) for more than one.
