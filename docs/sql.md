# SQL templates

Some queries are easier to write as plain SQL: a report with window functions,
a bulk update with `FROM`, a recursive CTE. Pushing them through the query
builder only makes them longer.

This layer lets you keep that SQL in files, binds the values for you, and
returns rows through the same methods a query has. It needs an extra:

```console
$ pip install "sqlakit[sql]"
```

Templates are `Jinja` files, rendered by
[jinja2sql](https://github.com/antonrh/jinja2sql). Every `{{ value }}` becomes
a bound parameter `:value__1`, so values never reach the SQL text itself.

## Where templates live

Set the template directory with `templates=` when you create the database:

```python
from pathlib import Path

from sqlakit import Database

BASE_DIR = Path(__file__).parent / "sql"

db = Database("postgresql+psycopg://localhost/app", templates=BASE_DIR)
```

If you use the registry, pass the same argument to `configure()`:

```python
from pathlib import Path

from sqlakit import db

BASE_DIR = Path(__file__).parent / "sql"

db.configure(
    "postgresql+psycopg://localhost/app",
    templates=BASE_DIR,
)
```

The rest of this page uses the first form, and everything works the same with
the second.

You address a template by its path from that root, extension included:
`db.sql("reports/by_team.sql")` reads `BASE_DIR/reports/by_team.sql`. You can
keep templates next to the code that uses them, or collect them all in one
directory. The root only decides where the lookup starts.

There are three calls, one per source of SQL, and all three read rows the
same way:

| call | the source of the SQL |
| --- | --- |
| `db.sql(name)` or `db.sql.from_file(name)` | a template under `templates=` |
| `db.sql.from_string(source)` | a string in the code, rendered the same way |
| `db.sql.from_statement(statement)` | a finished `SQLAlchemy` statement, unrendered |

`db.sql(name)` is a shorthand for `from_file`, and the one you'll use most of
the time. The other two are covered below.

If you need a customized `Jinja` environment, pass a `Templates` object:

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

Keyword arguments go into the template context. Rows arrive as `SQLAlchemy`
`Row` objects, which you can read by name or by position.

If you'd rather get rows as a type of your own, call `typed`:

```python
class TeamReport(BaseModel):
    team: str
    members: int


teams = db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
```

Pass the type of **one row**. The container depends on the method that runs
the query: `all()` returns a list and `one()` a single row. Rows are
validated by `pydantic`, which means a `pydantic` model, a dataclass, a
`TypedDict` and a plain `int` all work, and a row of the wrong shape raises
`ValidationError` right away instead of somewhere downstream.

A one-column row arrives as the value of that column:

```python
total = db.sql("reports/total.sql").typed(int).one()
```

`scalars` reads the first column of the result, and you don't have to declare
a type:

```python
total = db.sql("reports/total.sql").scalars().one()
names = db.sql("users/names.sql").scalars().all()
```

You call `typed` or `scalars` once. They aren't chained with each other, so
there's no call order to remember.

The methods that run the query are the same ones a query has: `all`, `first`,
`one`, `one_or_none`. They raise `SQLAlchemy`'s own `NoResultFound` and
`MultipleResultsFound`, the same way an ordinary result does. A query on a
model raises `InstanceNotFoundError` instead, with the model's name in the
message.

## Rows as models

When the SQL selects a model's columns, `from_sql` maps them onto it:

```python
users = User.query.from_sql("users/active.sql", team="red").all()
users = db.query(User).from_sql("users/active.sql", team="red").all()
```

Both lines do the same thing. The second one works without the
[model layer](models.md). The examples below use the shorter `User.query`
form.

Rows arrive as instances and land in the session. You can't narrow such a
query from code, because the file already decides what is selected and under
which conditions. A `where` on top of it raises `RawStatementError`, and the
message suggests moving the condition into the statement itself.

`from_sql` works with a file, which is the common case. For everything else
there's `from_statement`, which accepts both what the calls above return and
a statement built with `SQLAlchemy`:

```python
User.query.from_statement(db.sql.from_string("SELECT * FROM users LIMIT 10"))
User.query.from_statement(
    sa.text("SELECT * FROM users WHERE id = :id").bindparams(id=1)
)
```

One caveat: `SQLAKit` does not apply a model's
[`__query_filter__`](queries.md#soft-deletes) to your statement. If
you rely on that hook to hide soft-deleted rows or another tenant's rows,
repeat the condition in the template's own `WHERE`.

## Writing rows

```python
with db.transaction():
    archived = db.sql("users/archive_inactive.sql", before=cutoff).execute()

log.info("archived %d users", archived)
```

`execute()` runs a writing template and returns the number of affected rows.
Use it for `INSERT`, `UPDATE` and `DELETE`. Inside a transaction the write is
part of it and the block decides. In a block with no transaction the call
commits for itself, as ORM writes do.

## Walking a table

```python
with db.transaction():
    for batch in db.sql("exports/contacts.sql").chunks(1000):
        write(batch)
```

This is one query read in batches. The database keeps a cursor open for the
whole walk, so don't leave the transaction until you're done. If you'd rather
commit each batch separately, page the table with
[`cursor_page`](queries.md#walking-a-whole-table).

## SQL that is not in a file

A three-line query doesn't need a file of its own. `from_string` renders the
same way, and the source stays right in your code:

```python
query = db.sql.from_string("SELECT id FROM users WHERE team = {{ team }}", team="red")
ids = query.scalars().all()
```

This is the only call that works without `templates=`. Grep for it when you
want to find every place that builds SQL from strings.

!!! note "Placeholders belong to the template, not to the driver"

    You name values in `{{ }}` and pass them by keyword, both in `from_string`
    and in a file. Neither `SQLAlchemy`'s `:name` nor a driver's `?` or `%s`
    binds anything here: an unbound `:name` raises `StrayParameterError`
    during rendering, and `from_string` has no positional arguments to fill a
    `?` with.

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

`SQLAKit` renders nothing here: the parameters belong to the statement,
written in the regular `SQLAlchemy` syntax. The call adds the reading methods
(`typed`, `scalars`, `chunks` and the rest) on top of a statement you built
anywhere.

## Inside a template

`SQLAKit` binds every value as a parameter, whatever its type:

```sql
SELECT * FROM users WHERE team = {{ team }} AND joined_at > {{ since }}
```

A list reaches the database as a list, so `IN` works. An empty list
matches nothing and doesn't break the query:

```sql
SELECT * FROM users WHERE id IN {{ ids }}
```

`{{ ids | inclause }}` from `jinja2sql` expands the values one by one. It
reads the same, but doesn't accept an empty list.

Identifiers can't be bound as parameters. Quote a table or column name with
the `identifier` filter, and only let through names you've allowed yourself:

```sql
SELECT * FROM users ORDER BY {{ column | identifier }}
```

When the SQL needs to differ between databases, branch on `dialect`:

```sql
{% if dialect == "postgresql" %}
  SELECT * FROM users ORDER BY name COLLATE "C"
{% else %}
  SELECT * FROM users ORDER BY name
{% endif %}
```

Escape a colon that is part of the SQL itself. `SQLAlchemy` treats `:name` as
a parameter, so you have to write a JSON literal as `'{"a"\:1}'`. An unescaped
colon raises an error during rendering, with the file name in the message,
instead of failing when the query runs.

Some values need a type the driver can't guess on its own: NULL, a JSON
document, an array. Pass `sa.bindparam`, and the value goes through with its
type:

```python
db.sql("events/at.sql", at=sa.bindparam("at", when, type_=sa.DateTime(timezone=True)))
```

## Seeing the SQL

`statement` gives you the finished SQL, rendered and bound, without running
anything. You can check it in a test, or feed it to `EXPLAIN`:

```python
statement = db.sql("reports/by_team.sql", since=since).statement
```

`SQLAKit` adds the template name to the SQL as a comment, so a slow query log,
a [recording](debugging.md) and `pg_stat_statements` all show the source file
of each query:

```sql
/* reports/by_team.sql */
SELECT team, count(*) AS members
FROM users
WHERE joined_at > :since
GROUP BY team
```

## Checking the templates

`SQLAKit` only reads a template when it's first used, and that's a late
moment to find a typo. Call `check()` at startup, next to the rest of your
wiring:

```python
db.sql.check()
```

It compiles every `.sql` template under the roots you configured. A broken one
raises `TemplateSyntaxError` with the file and the line, so you find out at
startup rather than the first time someone uses that template.

## Templates under `asyncio`

The same methods in `sqlakit.asyncio`, awaited:

```python
teams = await db.sql("reports/by_team.sql", since=since).typed(TeamReport).all()
users = await User.query.from_sql("users/active.sql", team="red").all()

async for batch in db.sql("exports/contacts.sql").chunks(1000):
    await write(batch)
```

The `await` goes where the query runs. The rest is identical in both APIs.
Building a query with `db.sql(...)` or `from_string`, calling `check`, and
reading `statement` all stay synchronous, so you can pass a rendered template
to `from_statement` in either API.

Templates render synchronously. `Templates` rejects an async filter as soon
as the object is created, rather than putting a coroutine into your query in
place of a value:

```python
Templates(BASE_DIR, filters={"rate": fetch_rate})  # raises AsyncFilterError
```

If a template needs data from the network or from the database, fetch it with
`await` beforehand and pass the finished value in.

## Limits

A template is a whole statement, so there's nothing left to narrow. `where`,
`order_by` and `page` aren't available and raise `RawStatementError`. Paginate
in the SQL itself, or read the rows with a query.

Next: [queries](queries.md) for the queries the builder handles better, and
[debugging](debugging.md) for measuring what your templates cost.
