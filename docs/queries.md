# Queries

A query reads rows of one model, using the session of the open block. There
are several ways to get a query, and which one you use depends only on how
the model was declared.

## Database queries

A plain `SQLAlchemy` class isn't tied to this library in any way, so you build
the query from the database object:

```python
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database

db = Database("postgresql+psycopg://localhost/app")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str]
    is_active: Mapped[bool]


with db.connect():
    active = db.query(User).where(User.is_active).all()
```

The block binds the session the query runs on. Every example below assumes an
open block, so the examples omit it from here on.

## Repository queries

If you'd rather keep persistence out of your models, give the database to a
repository. It uses the same builder:

```python
from datetime import datetime

import sqlalchemy as sa

from sqlakit import CursorPage, Database, Page
from sqlakit.orm import Query


class UserRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def query(self) -> Query[User]:
        return self.db.query(User)

    def by_email(self, email: str) -> User | None:
        return self.query.where(User.email == email).first()

    def page(self, *, limit: int, offset: int) -> Page[User]:
        return self.query.order_by("created_at.desc").page(limit=limit, offset=offset)

    def cursor_page(self, *, limit: int, cursor: str | None = None) -> CursorPage[User]:
        return self.query.order_by("created_at.desc").cursor_page(
            limit=limit, cursor=cursor
        )

    def last_ordered_before(self, when: datetime) -> list[User]:
        # Raw SQL, mapped back onto the model.
        return self.query.from_statement(
            sa.text("""
                SELECT users.* FROM users
                LEFT JOIN orders ON orders.user_id = users.id
                GROUP BY users.id
                HAVING coalesce(max(orders.placed_at), users.created_at) < :before
            """).bindparams(before=when)
        ).all()
```

The last method shows a query that is easier to write as SQL than through the
builder. `from_sql` does the same from a file, with the values bound: see
[SQL templates](sql.md).

Every example below uses `db.query(User)`. `SQLAKit` reads the hooks
(`__orderable__`, `__cursor_key__`, `__query_filter__`) from the mapped class
itself, so they work on any declarative class.

If you prefer to get a query straight from the class, use
[`Model.query`](models.md). It builds the same object.

Both approaches are shown in full in the [examples](examples.md), with
`SQLModel` classes and with a repository.

## Query building

Every method returns a new query, so you can keep one and branch from it:

```python
active = db.query(User).where(User.is_active)

active.count()
active.order_by(User.name).all()
active.filter_by(team="red").first()
```

| builds | |
| --- | --- |
| `where`, `filter_by` | narrow the selection |
| `join`, `outerjoin`, `select_from` | bring in other tables |
| `order_by`, `distinct`, `limit`, `offset` | shape the result |
| `order_by("name.desc")` | ordering by a string from the request |
| `group_by`, `having` | aggregate |
| `options`, `joinedload`, `selectinload`, `subqueryload`, `contains_eager` | load relationships |
| `with_for_update`, `execution_options` | how the statement runs |

| runs | |
| --- | --- |
| `get`, `get_one` | one row by primary key |
| `all`, `first`, `one`, `one_or_none` | rows as instances |
| `latest`, `earliest` | the row with the highest or lowest value of a column |
| `count`, `exists` | how many rows match, and whether any do |
| `page`, `cursor_page` | one page of them |
| `chunks` | every row, a batch at a time |
| `only_columns` | columns instead of instances |
| `from_statement`, `from_sql` | rows from SQL you wrote |
| `create`, `create_many` | write new rows |
| `update`, `delete` | write to every matching row |

For everything else, `query.select` returns the underlying `SQLAlchemy`
`Select`.

When a required row is missing, the error names the model. `get_one` and `one`
raise `InstanceNotFoundError`, and `one` and `one_or_none` raise
`MultipleInstancesFoundError` when more than one row matches. Both errors
carry the model's name:

```python
from sqlakit import InstanceNotFoundError

try:
    user = db.query(User).where(User.email == email).one()
except InstanceNotFoundError as error:
    raise HTTPException(404, f"{error.model} not found")
```

Both inherit from `SQLAlchemy`'s `NoResultFound` and `MultipleResultsFound`,
so any `except` clauses you already have keep working.

## One row by key

`get()` returns `None` when there is no such row, and `get_one()` raises
`InstanceNotFoundError`:

```python
db.query(User).get(1)
db.query(User).get_one(1)
```

Both find the row by primary key. If the query was narrowed with `where`,
they raise `KeyLookupError` instead of silently ignoring the condition. Loader
options and locks are kept:

```python
db.query(User).joinedload(User.team).get(1)
db.query(User).with_for_update().get_one(1)
```

Both check the session's identity map first. If the row is already loaded,
they return the same instance without touching the database. The second call
below runs no statement and returns the same object:

```python
user = db.query(User).get(1)
assert db.query(User).get(1) is user
```

The map holds instances with weak references, so this caching lasts only while
the instance is alive in your code. When you use the same row from several
places, most of those lookups never reach the database.

An instance keeps the values it was loaded with. A write from another
transaction doesn't update them. `refresh()` reads the current values:

```python
db.session.refresh(user)
```

A query always runs against the database, but it returns instances from the
identity map. Rows the session already holds arrive as the same objects,
with the attributes they had before. To get fresh values, call `refresh()` or
start a new transaction.

## Ordering

`order_by` behaves like the `SQLAlchemy` one, so columns work as usual:

```python
db.query(User).order_by(User.created_at.desc(), User.id.asc())
db.query(User).order_by(sa.nulls_last(User.sent_at))
```

It also accepts an ordering string: `name`, `name.desc` or
`name.desc.nulls_last`. The default direction is ascending. Pass several, and
the rows are ordered by each in turn:

```python
sort = "created_at.desc"  # e.g. a query parameter

db.query(User).order_by(sort).page(limit=20, offset=40)
db.query(User).order_by("team", "created_at.desc").cursor_page(limit=20)
```

Strings and columns can be mixed in one call, for example a requested ordering
plus a default one:

```python
db.query(User).order_by(sort, User.id)
```

`SQLAKit` checks the field name against the model's allowed fields, and never
pastes it into the SQL. An unknown field raises `UnknownOrderFieldError` and
never reaches the database. `SQLAKit` skips `None` and unpacks a list, so you
can pass the ordering in whatever shape it arrived:

```python
db.query(User).order_by(sort)  # a string, or None when the client sent nothing
db.query(User).order_by(["team", "name.desc"])  # or several
```

A name is matched whichever case convention it arrives in, so `userName` finds
the `user_name` a model declares. A model offering both spellings is matched
exactly.

`nulls` says where the rows with no value go, `first` or `last`:

```python
db.query(User).order_by(sort, nulls="last").page(limit=20)
```

Without it the database decides, and they disagree:

| | `ASC` | `DESC` |
| --- | --- | --- |
| `SQLite`, `MySQL` | `NULL` first | `NULL` last |
| `PostgreSQL` | `NULL` last | `NULL` first |

A sort string that names its own (`sent_at.desc.nulls_first`) wins over it, and
so does a field the model declared as `sa.nulls_first(...)`. `MySQL` has no
`NULLS LAST` at all, so there the clause comes out as `v IS NULL ASC, v ASC`.

`ignore_case` compares text without regard to case:

```python
db.query(User).order_by("name", ignore_case=True).page(limit=20)
```

Pass the names instead of `True` when the sort arrived as a list and only some
of it is compared that way:

```python
db.query(User).order_by(sort, ignore_case=["name"]).page(limit=20)
```

The database decides how. `SQLite` orders by `COLLATE NOCASE`, and everything
else by `lower(name)` until you name a collation:

```python
sqlakit.CASE_INSENSITIVE_COLLATIONS["postgresql"] = "und-ci-ai"
```

Name one if the column has a collation of its own. `lower()` ignores it, and
with it the index and the accent folding: `résumé` and `resume` stay apart. A
column that is already case-insensitive needs no `ignore_case` at all.

A cursor cannot page either form, so use `ignore_case` with `page`.

By default you can order by **every mapped column**, and `__orderable__`
narrows that to the names an API may send:

```python
class User(Base):  # any mapped class
    __orderable__ = ("name", "created_at")
```

When a field isn't a plain column of the model, declare `__orderable__` as a
classmethod. This covers computed values, case-folded columns, and columns
from other tables.

```python
class User(Base):
    @classmethod
    def __orderable__(cls) -> Mapping[str, Any]:
        return {
            "created_at": cls.created_at,
            "name": sa.func.lower(cls.name),  # case-insensitive
            "last_seen_at": sa.nulls_last(cls.last_seen_at),  # never seen sort last
            "orders": _order_count_of(cls),  # a correlated subquery
        }
```

`__orderable__` replaces the columns rather than adding to them, so that a
sortable API can be narrowed to a few names. To add instead, start from
`orderable_columns`:

```python
from sqlakit import OrderBy, orderable_columns


class User(Base):
    @classmethod
    def __orderable__(cls) -> Mapping[str, Any]:
        return {
            **orderable_columns(cls),
            "team": OrderBy(Team.name, join=cls.team),
        }
```

Calling `orderable` there recurses: it reads the `__orderable__` that is
running.

You can't order by a field missing from the dict. Its keys are the full list
of orderings your API supports, written next to the model.

A name in the tuple with no mapped column behind it raises
`InvalidOrderFieldError`. This error means the declaration is wrong, not the
request, so it differs from the error for a bad ordering string. A field
wrapped in `sa.nulls_last()` or `sa.nulls_first()` keeps that placement under
any direction, unless the ordering string sets another.

### Fields of another table

Point the field at another table and the query adds the join automatically.
The join is added once, no matter how many fields use it, and not at all when
the query already contains it:

```python
from sqlakit import OrderBy


class User(Base):
    @classmethod
    def __orderable__(cls) -> Mapping[str, Any]:
        return {
            "name": cls.name,
            "team": OrderBy(Team.name, join=cls.team),
        }
```

```python
db.query(User).order_by("team").page(limit=20)
db.query(User).join(Team).where(Team.active).order_by("team").page(limit=20)
```

A column of another table needs no `OrderBy` at all. Name it, and the table is
joined on the key between them:

```python
{"opens": Stats.opens, "clicks": Stats.clicks}
```

A relationship of the model that reaches that table is used instead of the key,
condition and all. A view carrying no key is reachable that way:

```python
class Campaign(Model):
    analytics: Mapped[Analytics] = relationship(
        primaryjoin=lambda: sa.and_(
            Campaign.id == sa.orm.foreign(Analytics.campaign_id),
            Analytics.kind == "email",
        ),
        viewonly=True,
    )

    @classmethod
    def __orderable__(cls) -> Mapping[str, Any]:
        return {"bounces": Analytics.bounces}
```

`OrderBy` is for the rest: an alias, a subquery, or a table two relationships
reach.

Either way the join is an outer one, so the rows with nothing on the other side
come back as well, where `nulls` puts them. `OrderBy(..., outer=False)` drops
them instead.

A table named by several fields is joined once, so two fields that join it on
different conditions raise `ConflictingJoinError`. Give each an alias, and each
gets a join of its own:

```python
month = sa.orm.aliased(Stats)
week = sa.orm.aliased(Stats)

{
    "monthly": OrderBy(
        month.opens,
        join=month,
        on=sa.and_(month.campaign_id == cls.id, month.period == "month"),
    ),
    "weekly": OrderBy(
        week.opens,
        join=week,
        on=sa.and_(week.campaign_id == cls.id, week.period == "week"),
    ),
}
```

Only join relationships that match one row. A collection multiplies the rows,
and the page count comes out wrong. For a collection, join a subquery that
aggregates it, with an explicit `on`:

```python
counts = (
    sa.select(User.team_id, sa.func.count().label("members"))
    .group_by(User.team_id)
    .subquery()
)

# Declared on `Team`: each team has one count.
{"members": OrderBy(counts.c.members, join=counts, on=counts.c.team_id == cls.id)}
```

Cursor pagination doesn't work with joined fields: the cursor reads values
from the returned rows, and a row carries neither a joined column nor an
aggregate. Use `page` for these, and keep `cursor_page` for the model's own
columns.

## Pagination

Both kinds require an ordering. Without one the database returns rows in no
fixed order, and pages start repeating and skipping rows. A page with no
`order_by` raises `UnorderedPageError`.

Both also append a unique key to the ordering. Otherwise rows with equal
ordering values could land on two pages at once, or on none.

### Limit-offset pagination

Use it when page numbers matter and you need the total:

```python
page = db.query(User).order_by(User.name).page(limit=20, offset=40)

page.items  # 20 users
page.total  # how many match altogether
page.limit  # 20
page.offset  # 40
page.has_next  # whether there is another page
```

The total costs a separate `SELECT count(*)` over the whole selection, and the
database still walks the rows the offset skips. Both get more expensive as the
page number and the table grow.

A list with only next and previous buttons doesn't need a total. Pass
`total=False` to skip the counting query: the page reads one extra row and
computes `has_next` from it.

```python
page = db.query(User).order_by(User.name).page(limit=20, offset=40, total=False)

page.items  # 20 users, in one statement
page.total  # None, the count query was skipped
page.has_next  # whether there was a 21st row
```

A type checker knows which of the two you asked for. `page(limit=20)` returns a
`Page[User]`, whose `total` is an `int`, and `page(total=False)` returns a
`Page[User, None]`, so code that reads `total` is asked about it only where
there is one:

```python
def render(page: Page[User]) -> str:
    return f"{len(page.items)} of {page.total}"  # an int, no check needed
```

`UncountedPage[User]` is the other one, named for what a signature means by it:

```python
from sqlakit import UncountedPage


def feed(page: UncountedPage[User]) -> list[User]:
    return list(page.items)  # `page.total` is None, and the type says so
```

### Cursor pagination

Use it for feeds, APIs, and anything scrolled. The cost does not grow with
depth, and rows inserted in the meantime do not shift the window.

```python
page = db.query(User).order_by(User.created_at.desc()).cursor_page(limit=20)

page.items  # the first 20
page.next_cursor  # an opaque string, or None at the end
page.previous_cursor  # None on the first page
page.has_next
page.has_previous
```

To read a neighbouring page, pass either cursor to the next call. One
parameter serves both directions, because the direction is encoded in the
cursor itself.

```python
older = (
    db.query(User)
    .order_by(User.created_at.desc())
    .cursor_page(limit=20, cursor=page.next_cursor)
)
newer = (
    db.query(User)
    .order_by(User.created_at.desc())
    .cursor_page(limit=20, cursor=older.previous_cursor)
)
```

Paging backwards works the same way. `SQLAKit` reads the rows nearest before
the cursor and reverses them, so the page arrives in its usual order.

Both cursors point at rows of the page that produced them, so an empty page
has neither. Keep the cursor that got you there.

Order the query the same way every time. A cursor used with a different
ordering raises `InvalidCursorError`, and so does a cursor from another
source.

`SQLAKit` appends the model's key to the ordering. Otherwise rows sharing a
`created_at` could land on both sides of the boundary. Usually that is the
primary key. When the index behind the query ends on another unique column,
declare it:

```python
class User(Base):
    @classmethod
    def __cursor_key__(cls) -> Sequence[InstrumentedAttribute[Any]]:
        return (cls.public_id,)
```

Order by the model's own columns, and only by ones that are never NULL. The
cursor reads its values from the returned rows, so the ordering must
consist of columns present on those rows. `sa.text("name")`, an expression
such as `func.lower(...)`, or a column of a joined table raises
`UncomparableOrderingError`. The column's type does not matter: a `str` pages
as well as an `int`. A row with NULL in an ordering column raises
`NullCursorValueError`, because a comparison against NULL matches nothing and
the page has nowhere to start.

### Row transformation

`map` works on both kinds of page and keeps the totals and the cursors:

```python
page = db.query(User).order_by(User.name).page(limit=20).map(UserResponse.from_user)
page = (
    db.query(User).order_by(User.id).cursor_page(limit=20).map(UserResponse.from_user)
)
```

`map_all` passes the whole list in one call, for work done on all items at
once. The totals and the cursors belong to the page's rows, so a transform has
to return one item per row. A different count raises `PageItemsMismatchError`:

```python
page = page.map_all(lambda users: serialize_many(users))
```

If the transformation needs an `await`, run it yourself and pass the finished
items to `with_items`:

```python
page = page.with_items(await serialize(page.items))
```

## Table iteration {#walking-a-whole-table}

For a job that visits every row, you don't need pages or `all()`. `chunks`
reads one statement in batches and holds one batch in memory at a time:

```python
with db.transaction():
    for users in db.query(User).where(User.is_active.is_(False)).chunks(1000):
        send_reminder(users)
```

```python
async with db.transaction():
    async for users in db.query(User).where(User.is_active.is_(False)).chunks(1000):
        await send_reminder(users)
```

The database holds one cursor open for the whole read, so the walk is one
transaction, and a commit halfway through ends it. To commit each batch
instead, page the table. Every page is a new statement:

```python
cursor = None
while True:
    page = db.query(User).order_by("id").cursor_page(limit=1000, cursor=cursor)
    with db.transaction():
        send_reminder(page.items)
    cursor = page.next_cursor
    if cursor is None:
        break
```

`chunks` reads through `yield_per`, and `SQLAlchemy` does not allow that
together with a `joinedload` of a collection. Such a loader has to deduplicate
and buffer the rows, which streaming doesn't allow, so the call raises
`InvalidRequestError`. Load collections here with `selectinload`.

## Columns instead of instances

```python
names = db.query(User).order_by(User.name).only_columns(User.name).all()
pairs = db.query(User).only_columns(User.id, User.name).all()
counts = (
    db.query(User).group_by(User.team).only_columns(User.team, sa.func.count()).all()
)
```

One column arrives as plain values, several as tuples. The result can still
be narrowed with `where`, `order_by`, `distinct`, `limit` and `offset`, and
`order_by` accepts the model's ordering strings here as well:

```python
db.query(User).only_columns(User.name).order_by("created_at.desc").all()
```

## Row creation

```python
with db.transaction():
    user = db.query(User).create(name="ada", team="red")
```

The row goes through the session, so defaults, relationships and the identity
map behave the same as for anything else added to it. Any mapped class works.

`create_many` writes a list of rows in one statement, without creating
instances:

```python
with db.transaction():
    db.query(User).create_many([{"name": "ada"}, {"name": "grace"}])
```

## Bulk updates and deletes

```python
with db.transaction():
    db.query(User).where(User.team == "red").update({"team": "green"})
    db.query(User).where(User.is_active.is_(False)).delete()
```

Both run one statement and return the number of affected rows. Only the
filtering stays: a query that also has `order_by`, `limit`, `offset` or
a join raises `BulkQueryError` instead of silently dropping them.

A write needs a block, like any query. Inside a transaction the commit is left
to the block. In `connect()` or `autocommit()`, where there is no transaction,
the write commits immediately. With no block there is no session, and the
call raises `MissingSessionError`.

## Custom query methods

If you use a query often, give it a name and a subclass. You build it like an
ordinary one:

```python
from typing import Self

from sqlakit.orm import Query


class UserQuery(Query[User]):
    def active(self) -> Self:
        return self.where(User.is_active.is_(True))


UserQuery(User, db).active().order_by(User.name).page(limit=20)
```

Your methods chain with the built-in ones in any order, because each returns
the same class. `db.query()` always returns a plain `Query`, so a repository
has to build the subclass itself. Putting one on a model class is covered in
the [model layer](models.md#adding-methods-to-a-models-query).

Build on `where` and the other builder methods rather than on `self.select`,
because they carry the model, the filter and the database with them. When a
method needs something the builders do not cover, `with_select` takes a finished
statement and returns a query:

```python
class UserQuery(Query[User]):
    def sampled(self, rows: int) -> Self:
        return self.with_select(self.select.suffix_with(f"TABLESAMPLE ({rows})"))
```

Name the model in the subclass, as in `Query[User]`, and `all()`, `first()`
and `page()` return typed results. `db.query(User)` infers the type from its
argument, so you don't need an annotation there.

An async query is the same class from `sqlakit.asyncio.orm`, subclassed the
same way. Only the methods that run the statement are awaited.

## Soft deletes {#soft-deletes}

A model that hides rows, most often soft-deleted ones, declares the rule once:

```python
class User(Base):
    deleted_at: Mapped[datetime | None]

    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return cls.deleted_at.is_(None)
```

The filter applies to everything the query builds: `all`, `count`, `page`,
`cursor_page`, and `get`, which returns `None` for a hidden row. Bulk `update`
and `delete` apply it too, and touch only the rows the query can see.

`unfiltered()` removes the filter, for reads and writes alike:

```python
db.query(User).unfiltered().all()  # hidden rows as well
db.query(User).unfiltered().get(2)  # by key, hidden or not
db.query(User).unfiltered().update({"team": "green"})  # every matching row
```

For rows hidden because they were deleted, use
[`SoftDeletes`](models.md#soft-deletes) rather than this hook. It has a
separate switch: `with_deleted()` lifts only the soft-delete filter and leaves
a tenant rule in this hook untouched.

The filter does not apply to SQL you wrote yourself. `from_statement` and
`from_sql` run the statement as written, so a template that hides rows has to
do it in its own `WHERE`.

The same hook on a base class filters every model under it, so a soft delete
or a tenant rule stays out of the call sites:

```python
class Base(DeclarativeBase):
    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(default=None)

    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return cls.deleted_at.is_(None)
```

The hook is a classmethod and runs on every read, not once at import. So it
sees whatever the current request put into the context:

```python
class Base(DeclarativeBase):
    __abstract__ = True

    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return cls.tenant_id == current_tenant.get()
```

A model that needs both rules, its own and the base's, combines them:

```python
class User(Base):
    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return sa.and_(super().__query_filter__(), cls.is_active.is_(True))
```

The filter works at the statement level and does not affect objects already in
the session. An instance loaded before the rule applied, or through
`unfiltered()`, can still be changed and saved. If you need to prevent that,
check it where the write happens. The filter can't.

## Raw SQL

```python
users = db.query(User).from_statement(sa.text("SELECT * FROM users WHERE ...")).all()
users = db.query(User).from_sql("users/active.sql", team="red").all()
```

The rows still arrive as model instances. You can't build on top of such a
query: the SQL decides what is selected, so a `where` or a `page` raises
`RawStatementError`.

`from_sql` reads the statement from a template and binds the values. See
[SQL templates](sql.md) for details, including rows that belong to no model.

`__query_filter__` does not apply in either case. See
[Soft deletes](#soft-deletes).

## Async queries

The same layer in `sqlakit.asyncio.orm`, awaited:

```python
page = await db.query(User).order_by("name").page(limit=20)
page = await db.query(User).order_by("name").cursor_page(limit=20)
user = await db.query(User).get_one(1)
```

The builder methods stay synchronous: nothing runs until you ask for rows.

Next: [SQL templates](sql.md) for queries that are easier to write as SQL, or
[models](models.md) for instances that save themselves.
