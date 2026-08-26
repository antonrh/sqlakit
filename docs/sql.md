# SQL templates

Some queries read better as SQL. A report with window functions, a bulk update
with `FROM`, a recursive CTE: through the builder they get longer rather than
clearer.

This layer keeps that SQL in files, binds the values, and returns rows through
the same methods a query does. It needs an extra:

```console
$ pip install "sqlakit[sql]"
```

Templates are written in `Jinja`, and
[jinja2sql](https://github.com/antonrh/jinja2sql) runs them. A `{{ value }}`
reaches the SQL as `:value__1`, with the value itself travelling separately.

## Where templates live

The directory of files is named when the database is built, through
`templates=`:

```python
from pathlib import Path

from sqlakit import Database

BASE_DIR = Path(__file__).parent / "sql"

db = Database("postgresql+psycopg://localhost/app", templates=BASE_DIR)
```

The registry takes the same thing in `configure()`, which is the only place it
has for it:

```python
from pathlib import Path

from sqlakit import db

BASE_DIR = Path(__file__).parent / "sql"

db.configure(
    "postgresql+psycopg://localhost/app",
    templates=BASE_DIR,
)
```

The page goes on with the first of those, and reads the same for the second.

A template is named by its path from that root, extension included, so
`db.sql("reports/by_team.sql")` reads `BASE_DIR/reports/by_team.sql`. Keep
templates next to the code that uses them, or gather them in one directory: the
root only says where looking starts.

Where the SQL comes from depends on the call, and all three read rows the same
way:

| call | where the SQL comes from |
| --- | --- |
| `db.sql(name)` or `db.sql.from_file(name)` | a template under `templates=` |
| `db.sql.from_string(source)` | a string in the code, rendered the same way |
| `db.sql.from_statement(statement)` | a finished `SQLAlchemy` statement, unrendered |

`db.sql(name)` is the short form of `from_file`, and the one to reach for first.
The other two are covered below, each in its place.

For a `Jinja` environment of your own, pass a `Templates` object:

```python
from sqlakit.sql import Templates

db = Database(
    DB_URL,
    templates=Templates(BASE_DIR, auto_reload=DEBUG, filters={"money": as_money}),
)
```

## Reading rows

```python
rows = db.sql("reports/by_team.sql", since=since).all()
```

Keyword arguments go into the template context. Rows come back as `SQLAlchemy`
`Row` objects, readable by name and by position.

For a type of your own, call `typed`:

```python
class TeamReport(BaseModel):
    team: str
    members: int


teams = db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
```

Name the type of **one row**. The container is chosen by the method that runs
the query: `all()` returns a list, `one()` a single row. Rows are checked by
`pydantic`, so a model of its own, a dataclass, a `TypedDict` and an `int` all
work, and a row of the wrong shape raises `ValidationError` right here rather
than somewhere downstream.

A one-column row arrives as the value of that column:

```python
total = db.sql("reports/total.sql").typed(int).one()
```

`scalars` reads the first column of whatever came back, and needs no type named:

```python
total = db.sql("reports/total.sql").scalars().one()
names = db.sql("users/names.sql").scalars().all()
```

Each of these is called once, with nothing built on top of typed rows. There is
no call order to remember, because there is no second call.

The methods that run the query are the ones a query already has: `all`, `first`,
`one`, `one_or_none`. They raise `SQLAlchemy`'s own `NoResultFound` and
`MultipleResultsFound`, as an ordinary result does. A query on a model raises
`InstanceNotFoundError` instead, which names the model.

## Rows as models

When the SQL selects a model's columns, `from_sql` maps them onto it:

```python
users = User.query.from_sql("users/active.sql", team="red").all()
users = db.query(User).from_sql("users/active.sql", team="red").all()
```

Both lines read the same thing. The second does without the
[model layer](models.md), and every example below shortens to it.

Rows come back as instances and land in the session. Narrowing such a query from
code is no longer on the table, because the file decides the selection and the
conditions. A `where` on top of it raises `RawStatementError`, which also
suggests moving the condition into the statement itself.

`from_sql` works with a file, which is the common case. Everything else goes
through `from_statement`, which takes both what the calls above returned and
what `SQLAlchemy` built:

```python
User.query.from_statement(db.sql.from_string("SELECT * FROM users LIMIT 10"))
User.query.from_statement(
    sa.text("SELECT * FROM users WHERE id = :id").bindparams(id=1)
)
```

One thing to watch: a model's
[`__query_filter__`](queries.md#hiding-rows-for-good) is not applied to your
statement. If that hook hides soft-deleted rows or another tenant's rows, hide
them again in the template's own `WHERE`.

## Writing rows

```python
with db.transaction():
    archived = db.sql("users/archive_inactive.sql", before=cutoff).execute()

log.info("archived %d users", archived)
```

`execute()` runs a writing template and returns how many rows it touched. It is
what `INSERT`, `UPDATE` and `DELETE` want as well.

## Walking a table

```python
with db.transaction():
    for batch in db.sql("exports/contacts.sql").chunks(1000):
        write(batch)
```

This is one query read in batches. The database holds a cursor open for the
whole walk, so do not leave the transaction. To commit each batch instead, page
the table with [`cursor_page`](queries.md#walking-a-whole-table).

## SQL that is not in a file

Three lines do not need a file of their own. `from_string` renders the same way,
and keeps the source in front of you:

```python
query = db.sql.from_string("SELECT id FROM users WHERE team = {{ team }}", team="red")
ids = query.scalars().all()
```

This is the only call that works without `templates=`. It is also the one to
grep for when you want to know where SQL is assembled from strings.

!!! note "Placeholders belong to the template, not to the driver"

    Values are named in `{{ }}` and passed by keyword, in `from_string` and in a
    file alike. Neither `SQLAlchemy`'s `:name` nor a driver's `?` or `%s` binds
    anything here: an unbound `:name` raises `StrayParameterError` while
    rendering, and `from_string` takes no positional arguments to fill a `?`
    with.

```python
db.sql.from_string("SELECT * FROM users WHERE id = {{ id }}", id=1)  # binds 1
db.sql.from_string("SELECT * FROM users WHERE id = :id", id=1)  # raises
```

`from_statement` takes SQL that `SQLAlchemy` built, and reads it the same way:

```python
statement = sa.text("SELECT * FROM users WHERE id = :user_id").bindparams(user_id=1)

user = db.sql.from_statement(statement).typed(User).one()
totals = db.sql.from_statement(sa.select(Sale.team, sa.func.sum(Sale.amount))).all()
```

Nothing is rendered here, so the parameters belong to the statement and are
written the way `SQLAlchemy` writes them. The call adds the reading and nothing
else: `typed`, `scalars`, `chunks` and the rest, over a statement built
anywhere.

## Inside a template

Any value travels as a parameter, whatever its type:

```sql
SELECT * FROM users WHERE team = {{ team }} AND joined_at > {{ since }}
```

A list reaches the database as a list, so `IN` works. An empty one matches
nothing without breaking the query:

```sql
SELECT * FROM users WHERE id IN {{ ids }}
```

`{{ ids | inclause }}` from `jinja2sql` writes the values out one by one. It
reads the same, and takes no empty list.

Identifiers cannot be bound. Quote a table or column name through `identifier`,
and let through only the names you allowed yourself:

```sql
SELECT * FROM users ORDER BY {{ column | identifier }}
```

When the SQL has to differ between databases, branch on `dialect`:

```sql
{% if dialect == "postgresql" %}
  SELECT * FROM users ORDER BY name COLLATE "C"
{% else %}
  SELECT * FROM users ORDER BY name
{% endif %}
```

Escape a colon that belongs to the SQL itself. `SQLAlchemy` reads `:name` as a
parameter, so a JSON literal has to be written `'{"a"\:1}'`. Left unescaped, the
template complains while rendering and names the file, rather than failing when
it runs.

Some values want a type the driver will not guess: NULL, a JSON document, an
array. Pass `sa.bindparam`, and the value goes through with its type:

```python
db.sql("events/at.sql", at=sa.bindparam("at", when, type_=sa.DateTime(timezone=True)))
```

## Seeing the SQL

`statement` hands back the finished SQL, rendered and bound, and runs nothing.
Check it in a test, or feed it to `EXPLAIN`:

```python
statement = db.sql("reports/by_team.sql", since=since).statement
```

The template's name goes into the SQL as a comment, so a slow query log, a
[recording](debugging.md) and `pg_stat_statements` all show which file a query
came from:

```sql
/* reports/by_team.sql */
SELECT team, count(*) AS members
FROM users
WHERE joined_at > :since
GROUP BY team
```

## Checking the templates

A template is read when something asks for it, and finding a typo at that moment
is late. Call `check()` at startup, next to the rest of the wiring:

```python
db.sql.check()
```

It compiles every `.sql` template it finds under the roots you gave. A broken
one raises `TemplateSyntaxError` naming the file and the line, and it happens at
startup rather than the minute someone first wants that template.

## Templates under `asyncio`

The same methods in `sqlakit.asyncio`, awaited:

```python
teams = await db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
users = await User.query.from_sql("users/active.sql", team="red").all()

async for batch in db.sql("exports/contacts.sql").chunks(1000):
    await write(batch)
```

The `await` goes where the query runs; the rest is identical across both APIs.
Building a query through `db.sql(...)` and `from_string`, along with `check` and
reading `statement`, stays synchronous, so a rendered template suits
`from_statement` in either one.

Templates render synchronously. An async filter is rejected by `Templates` as it
is built, rather than putting a coroutine into your query in place of a value:

```python
Templates(BASE_DIR, filters={"rate": fetch_rate})  # raises AsyncFilterError
```

If a template wants data from the network or from the database, fetch it with
`await` beforehand and pass the finished value in.

## Limits

A template is a whole statement, so there is nothing in it to narrow. `where`,
`order_by` and `page` are unavailable and raise `RawStatementError`. Page in the
SQL itself, or read the rows with a query.

Next: [queries](queries.md) for the rows a builder handles better, and
[debugging](debugging.md) for what a template costs.
