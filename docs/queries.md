# Queries

A query reads rows of one model on the session of the open block. There is more
than one way to reach it, and which one depends only on how the model was
declared.

## From the database

A plain `SQLAlchemy` class knows nothing about this library, so the database
builds the query over it:

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

The block binds the session the query will run on. Every example below sits
inside a block, and the page stops saying so from here on.

## In a repository

An application that keeps saving out of its models hands the database to a
repository, which reaches the same builder:

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
        # SQL of its own, mapped back onto the model.
        return self.query.from_statement(
            sa.text("""
                SELECT users.* FROM users
                LEFT JOIN orders ON orders.user_id = users.id
                GROUP BY users.id
                HAVING coalesce(max(orders.placed_at), users.created_at) < :before
            """).bindparams(before=when)
        ).all()
```

That last method is the case where the builder gets longer rather than clearer.
`from_sql` does the same thing from a file, with the values bound: see
[SQL templates](sql.md).

Every example below is written through `db.query(User)`. The hooks are read off
the mapped class itself, so `__orderable__`, `__cursor_key__` and
`__query_filter__` work on any declarative class.

If reaching a query straight off the class suits you better, there is
[`Model.query`](models.md), which builds the very same object.

In [examples](examples.md) both are written out in full, with `SQLModel` classes
and with a repository.

## Building

Every method returns a new query, so one can be kept and branched from:

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
| `order_by("name.desc")` | ordering by a string the request carries |
| `group_by`, `having` | aggregate |
| `options`, `joinedload`, `selectinload`, `subqueryload`, `contains_eager` | load relationships |
| `with_for_update`, `execution_options` | how the statement runs |

| runs | |
| --- | --- |
| `get`, `get_one` | one row by primary key |
| `all`, `first`, `one`, `one_or_none` | rows as instances |
| `latest`, `earliest` | the row with the highest or lowest value of a column |
| `count`, `exists` | how many there are, and whether there are any |
| `page`, `cursor_page` | one page of them |
| `chunks` | every row, a batch at a time |
| `only_columns` | columns instead of instances |
| `from_statement`, `from_sql` | rows of SQL you wrote |
| `create`, `create_many` | write new rows |
| `update`, `delete` | write to every matching row |

Everything else lives in one attribute: `query.select` hands back the
`SQLAlchemy` `Select` that was assembled underneath.

The calls that require a row name the model that had none. `get_one` and `one`
raise `InstanceNotFoundError`, and `one` and `one_or_none` raise
`MultipleInstancesFoundError` when more than one row matches. Both errors carry
the model's name:

```python
from sqlakit import InstanceNotFoundError

try:
    user = db.query(User).where(User.email == email).one()
except InstanceNotFoundError as error:
    raise HTTPException(404, f"{error.model} not found")
```

Both inherit `SQLAlchemy`'s `NoResultFound` and `MultipleResultsFound`, so an
`except` you already wrote goes on working.

## One row by key

`get()` returns `None` when there is no such row, and `get_one()` raises
`InstanceNotFoundError`:

```python
db.query(User).get(1)
db.query(User).get_one(1)
```

Both look the row up by primary key. A query narrowed by `where` is not ignored
by them but rejected, with `KeyLookupError`. The loading you asked for and a
lock do carry over:

```python
db.query(User).joinedload(User.team).get(1)
db.query(User).with_for_update().get_one(1)
```

Both look in the session's identity map first. When the row is already there,
they hand back the same instance and never reach the database. The second call
below runs no statement at all and returns that same object:

```python
user = db.query(User).get(1)
assert db.query(User).get(1) is user
```

The map holds them with weak references, so the saving lasts exactly as long as
the instance lives in your code. Where one row is touched from several places,
that cuts out most of the reads.

The values in an instance belong to the moment it was loaded, and a write from
another transaction does not reach them. `refresh()` reads the current ones:

```python
db.session.refresh(user)
```

The query itself always goes to the database, but it hands out instances from
the identity map. Rows the session already holds come back as the same objects,
with the attributes they had before. Fresh values come from `refresh()` or from
a new transaction.

## Ordering

`order_by` here is the one from `SQLAlchemy`, so columns work as they always do:

```python
db.query(User).order_by(User.created_at.desc(), User.id.asc())
db.query(User).order_by(sa.nulls_last(User.sent_at))
```

It takes an ordering string as well: `name`, `name.desc` or
`name.desc.nulls_last`. Ascending unless the string says otherwise.

```python
sort = "created_at.desc"  # a query parameter, say

db.query(User).order_by(sort).page(limit=20, offset=40)
db.query(User).order_by("team", "created_at.desc").cursor_page(limit=20)
```

Both kinds live together in one call, so a default ordering and a requested one
stand side by side:

```python
db.query(User).order_by(sort, User.id)
```

The name is looked up among what the model offers rather than pasted into the
SQL. A field you never meant to order by raises `UnknownOrderFieldError` and
never reaches the database. `None` is skipped and a list is unpacked, so an
ordering can be passed in whatever shape it arrived:

```python
db.query(User).order_by(sort)  # a string, or None when the client sent nothing
db.query(User).order_by(["team", "name.desc"])  # or several
```

`ci_fields` names the fields compared without case:

```python
db.query(User).order_by("name", ci_fields=["name"]).page(limit=20)
```

The ordering then goes by `lower(name)`, which a cursor cannot page. Take it
with `page`, or fold the case in `__orderable__`, where it can be indexed.

By default a model offers **every mapped column it has**, and `__orderable__`
narrows that to the names an API may send:

```python
class User(Base):  # any mapped class
    __orderable__ = ("name", "created_at")
```

When a field is not a plain column of the model, make it a classmethod. That is
how a computed value, a case-folded one, or a column of another table is named.

```python
class User(Base):
    @classmethod
    def __orderable__(cls) -> Mapping[str, Any]:
        return {
            "created_at": cls.created_at,
            "name": sa.func.lower(cls.name),  # without case
            "last_seen_at": sa.nulls_last(cls.last_seen_at),  # never seen, last
            "orders": _order_count_of(cls),  # a correlated subquery
        }
```

What the dict does not hold cannot be ordered by. Its keys are the list of
orderings your API has, written next to the model.

A name in the tuple with no mapped column behind it raises
`InvalidOrderFieldError`. The mistake there is in the declaration rather than in
the request, which is why the exception differs from the one a bad ordering
string gives. A field wrapped in `sa.nulls_last()` or `sa.nulls_first()` keeps
that placement under any ordering, until the ordering string names another.

### A field from another table

Name the table and the query joins it for you. Once, however many fields name
it, and not at all when the query already reaches it:

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

Join a relationship with one row behind it. A collection multiplies the rows,
and the page counts them wrong. For one of those, join a subquery that
aggregates it, with the `on` it needs:

```python
counts = (
    sa.select(User.team_id, sa.func.count().label("members"))
    .group_by(User.team_id)
    .subquery()
)

# On `Team`, because the count belongs to that side.
{"members": OrderBy(counts.c.members, join=counts, on=counts.c.team_id == cls.id)}
```

A cursor pages none of this: it reads the values back off the rows, and a row
carries neither a joined column nor an aggregate. Take these with `page`, and
leave `cursor_page` to the model's own columns.

## Pagination

Both kinds need an ordering. Without one the database returns rows as it finds
them, and the pages start repeating and skipping them. Asking for a page with no
`order_by` raises `UnorderedPageError`.

Both also append the key that makes a row unique. Otherwise rows with equal
ordering values would land on two pages at once, or on none.

### Limit-offset pagination

Take it when page numbers matter and the total has to be known:

```python
page = db.query(User).order_by(User.name).page(limit=20, offset=40)

page.items  # 20 users
page.total  # how many match altogether
page.limit  # 20
page.offset  # 40
page.has_next  # whether there is another page
```

The total costs a `SELECT count(*)` of its own over the whole selection, and the
rows the offset skips are walked by the database anyway. The further the page
and the larger the table, the more both of those cost.

A list with next and previous buttons has no use for a total. Pass
`total=False`, and the page drops the counting query: it reads one row more than
it shows, and answers `has_next` from that.

```python
page = db.query(User).order_by(User.name).page(limit=20, offset=40, total=False)

page.items  # 20 users, in one statement
page.total  # None, because nothing counted them
page.has_next  # whether there was a 21st row
```

### Cursor pagination

Take it for feeds, APIs, and anything scrolled. The cost does not grow with
depth, and rows inserted meanwhile do not shift the window.

```python
page = db.query(User).order_by(User.created_at.desc()).cursor_page(limit=20)

page.items  # the first 20
page.next_cursor  # an opaque string, or None at the end
page.previous_cursor  # None on the first page
page.has_next
page.has_previous
```

To read a neighbouring page, hand either cursor back. One parameter serves both
buttons, since the direction is written into the cursor itself.

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

Paging backwards works the same way, in the other direction. The library takes
the rows nearest before the cursor and turns them back around, so the page
arrives in its usual order.

Both cursors point at rows of the page they came from, so an empty page yields
neither. Keep the cursor that brought you here.

Order the query the same way every time. A cursor read under a different
ordering raises `InvalidCursorError`, as does one that came from somewhere else.

The model's key is appended to the ordering, or rows sharing a `created_at`
would end up on both sides of the boundary. Usually that is the primary key, but
when the index behind the query ends on another unique column, name it:

```python
class User(Base):
    @classmethod
    def __cursor_key__(cls) -> Sequence[InstrumentedAttribute[Any]]:
        return (cls.public_id,)
```

Order by the model's own columns, and by ones that are never NULL. The cursor
reads its values back off the rows it returned, so the ordering has to hold
columns it will find there. `sa.text("name")`, an expression such as
`func.lower(...)`, or a column of a joined table raise
`UncomparableOrderingError`. The column's type has nothing to do with it: a
`str` pages as well as an `int`. A row with NULL in an ordering column raises
`NullCursorValueError`, since a comparison against NULL matches nothing and the
page has nowhere to start.

### Turning rows into something else

`map` is on both kinds of page, and it keeps the totals and the cursors:

```python
page = db.query(User).order_by(User.name).page(limit=20).map(UserResponse.from_user)
page = (
    db.query(User).order_by(User.id).cursor_page(limit=20).map(UserResponse.from_user)
)
```

`map_all` hands over the whole list in one call, for work that suits doing to
all of them at once:

```python
page = page.map_all(lambda users: serialize_many(users))
```

An awaited transformation does that itself and puts the items back:

```python
page = page.with_items(await serialize(page.items))
```

## Walking a whole table {#walking-a-whole-table}

A job that visits every row wants neither pages nor `all()`. `chunks` reads one
statement in batches and holds one at a time in memory:

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

The rows come off a cursor the database holds open, so the whole walk is one
transaction, and a commit halfway through ends it. To commit each batch instead,
page the table, where every page is a new statement:

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
together with a `joinedload` against a collection. Such a loader needs to unique
and buffer the rows, which streaming does not permit, so instead of a statement
you get `InvalidRequestError`. Load collections here with `selectinload`.

## Columns instead of instances

```python
names = db.query(User).order_by(User.name).only_columns(User.name).all()
pairs = db.query(User).only_columns(User.id, User.name).all()
counts = (
    db.query(User).group_by(User.team).only_columns(User.team, sa.func.count()).all()
)
```

One column comes back as its values, several as tuples. The result narrows
further through `where`, `order_by`, `distinct`, `limit` and `offset`, and the
names a model offers are taken by `order_by` here as well:

```python
db.query(User).only_columns(User.name).order_by("created_at.desc").all()
```

## Writing

```python
with db.transaction():
    user = db.query(User).create(name="ada", team="red")
```

The row goes through the session, so defaults, relationships and the identity
map behave as they do for anything else added to it. Any mapped class works.

`create_many` writes a list of rows in one statement and instantiates nothing:

```python
with db.transaction():
    db.query(User).create_many([{"name": "ada"}, {"name": "grace"}])
```

## Writing many rows

```python
with db.transaction():
    db.query(User).where(User.team == "red").update({"team": "green"})
    db.query(User).where(User.is_active.is_(False)).delete()
```

Both write in one statement and return how many rows they touched. Only the
narrowing carries over: a query that also holds `order_by`, `limit`, `offset` or
a join raises `BulkQueryError` rather than dropping them quietly.

A write wants a block, as any query does. Inside a transaction it leaves the
commit to the block, and in `connect()` or `autocommit()`, where there is no
transaction, it commits for itself. With no block there is no session, so the
call raises `MissingSessionError`.

## Adding methods to a query

A query you need often deserves a name and a subclass. It is built the way an
ordinary one is:

```python
from typing import Self

from sqlakit.orm import Query


class UserQuery(Query[User]):
    def active(self) -> Self:
        return self.where(User.is_active.is_(True))


UserQuery(User, db).active().order_by(User.name).page(limit=20)
```

Your methods chain with the built-in ones in any order, since each returns the
same class. `db.query()` always hands back a plain `Query`, so a repository
builds the subclass itself. Putting one on a model class is covered in the
[model layer](models.md#adding-methods-to-a-models-query).

Build on `where` and the other builders rather than on `self.select`, since they
carry the model, the filter and the database with them. When a method needs
something the builders do not cover, `with_select` takes a finished statement
and hands a query back:

```python
class UserQuery(Query[User]):
    def sampled(self, rows: int) -> Self:
        return self.with_select(self.select.suffix_with(f"TABLESAMPLE ({rows})"))
```

Name the model in the subclass, as in `Query[User]`, and `all()`, `first()` and
`page()` come back typed. `db.query(User)` takes the type from its argument, so
it needs no annotation.

An async query is the same class from `sqlakit.asyncio.orm`, subclassed the same
way. The `await` belongs to the methods that run the statement.

## Hiding rows for good {#hiding-rows-for-good}

A model that hides rows, most often soft-deleted ones, says so once:

```python
class User(Base):
    deleted_at: Mapped[datetime | None]

    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return cls.deleted_at.is_(None)
```

The filter reaches everything the query builds: `all`, `count`, `page`,
`cursor_page`, and `get`, which returns `None` for a hidden row. Bulk `update`
and `delete` carry it too, and touch only the rows the query can see.

`unfiltered()` steps around it, lifting the filter from reads and writes alike:

```python
db.query(User).unfiltered().all()  # hidden rows as well
db.query(User).unfiltered().get(2)  # by key, hidden or not
db.query(User).unfiltered().update({"team": "green"})  # every matching row
```

For rows hidden because they were deleted, take
[`SoftDeletes`](models.md#soft-deletes) rather than this hook. It has a switch
of its own, so `with_deleted()` lifts that one and leaves a tenant rule living
here alone.

The filter is not applied to SQL you wrote yourself. `from_statement` and
`from_sql` run the statement as it stands, so a template that has to hide rows
has to say so in its own `WHERE`.

The same hook on a base class filters every model under it. That is how a soft
delete or a tenant rule stays out of the call sites:

```python
class Base(DeclarativeBase):
    __abstract__ = True

    deleted_at: Mapped[datetime | None] = mapped_column(default=None)

    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return cls.deleted_at.is_(None)
```

The hook is a classmethod, called on every read rather than once at import. So
it sees whatever the request put into the context:

```python
class Base(DeclarativeBase):
    __abstract__ = True

    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return cls.tenant_id == current_tenant.get()
```

A model that wants both rules, its own and the base's, combines them:

```python
class User(Base):
    @classmethod
    def __query_filter__(cls) -> ColumnExpressionArgument[bool]:
        return sa.and_(super().__query_filter__(), cls.is_active.is_(True))
```

The filter works at the level of the statement and does not touch objects
already in the session. An instance loaded before the rule arrived, or through
`unfiltered()`, is one the session will let you change and save. Check that
where the writing happens rather than expecting it of the filter.

## Your own SQL instead of the builder

```python
users = db.query(User).from_statement(sa.text("SELECT * FROM users WHERE ...")).all()
users = db.query(User).from_sql("users/active.sql", team="red").all()
```

The rows still arrive as instances of the model. Adding to such a query is no
longer possible, because the SQL decides what is selected, and a `where` or a
`page` on top of it raises `RawStatementError`.

`from_sql` takes the statement from a template and binds the values. The details
are in [SQL templates](sql.md), along with rows that belong to no model.

`__query_filter__` is applied in neither case, since the statement runs as
written. A soft delete or a tenant rule has to go into the SQL itself.

## Queries under `asyncio`

The same layer in `sqlakit.asyncio.orm`, awaited:

```python
page = await db.query(User).order_by("name").page(limit=20)
page = await db.query(User).order_by("name").cursor_page(limit=20)
user = await db.query(User).get_one(1)
```

The builders stay synchronous: until you ask for rows, nothing runs.

Next: [SQL templates](sql.md) for the queries a builder makes longer, or
[models](models.md) for instances that save themselves.
