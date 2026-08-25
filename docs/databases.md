# The database

A `Database` holds the SQLAlchemy engine, its connection pool, and the context
the blocks bind into. On its own it connects to nothing. The engine arrives when
the first block asks for it.

```python
from sqlakit import Database

db = Database("postgresql+psycopg://localhost/app")
```

## A URL, or separate arguments

Settings usually arrive in pieces rather than as one string. Pass them as they
are, with nothing assembled by hand:

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

A password is the reason to prefer this. Passed as its own argument it is quoted
for you, and reads back as it went in. Put a password with special characters
into a URL string and nothing raises. SQLAlchemy splits the string on the first
`@`, and the rest of the password scatters into the wrong fields:

```python
import sqlalchemy as sa

url = sa.make_url("postgresql+psycopg://u:p@ss/word@h/app")

url.password  # 'p'
url.host  # 'ss'
url.database  # 'word@h/app'
```

Pass either a finished URL or the arguments separately. When both arrive, the
library answers with `ConflictingDatabaseUrlError` rather than guessing on your
behalf.

## An instance, or the registry

A database can live in an instance of your own or in the registry. The pages
here use both.

**Your own instance**, passed where it is needed:

```python
# app/db.py
from sqlakit import Database

db = Database(settings.DATABASE_URL)
```

Whoever needs it imports it from there, and a test can build a second one
without disturbing the first.

**The registry**, configured once at startup:

```python
# app/main.py
from sqlakit import db

db.configure(settings.DATABASE_URL)
```

Any module then does `from sqlakit import db` and reaches the same connections.
Reconfiguring is allowed until something connects, after which it raises
`DatabaseAlreadyConfiguredError` and `db.dispose()` has to come first.

`configure()` takes everything the constructor takes, separate arguments
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

The same registry holds more than one database: see
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

The registry says the same thing through `configure()`:

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

What you pass wins. The defaults, and the reasoning behind each, are in the
[reference](reference.md#defaults). `pool_size`, `max_overflow` and
`isolation_level` are left alone, since they depend on the worker count, the
database's limit and the dialect.

`templates=` says which directory [SQL templates](sql.md) read their files from.

## Starting and stopping

Connecting by hand is not something you do. Until code inside a block reaches
for the database, nothing opens.

```python
db.ping()  # whether the database answers, for a health endpoint
db.dispose()  # close every connection the pool holds, on shutdown
```

`dispose()` belongs wherever your framework shuts things down, which for FastAPI
is the lifespan; [Examples](examples.md) has it in place. A script that wants a
database for the length of one run can take `Database` as a context manager,
which disposes the engine on the way out:

```python
with Database(url) as db, db.transaction():
    backfill()
```

Next: [context](context.md) for the blocks that bind it, or
[multiple databases](routing.md) for more than one.
