---
name: migrate-from-jinja
description: "Move an application's SQL templates from Jinja (jinja2sql, the default engine of `Templates`) to SQLAKit's `tpl` templates: `:name` parameters and `tpl.` macros. Use when asked to migrate, convert or port a Jinja `.sql` template, a Jinja macro library or a custom filter to `tpl.` macros or `@sql_macro`, in SQLAKit or in a project that uses it. Covers the order of work, the Jinja to `tpl` mapping, the behaviour that changes quietly, and how to prove the new template returns what the old one did."
---

# Migrating templates from Jinja to `tpl`

A `tpl` template is SQL in the production dialect with `:name` parameters.
Whatever changes per call is a `tpl.<macro>(...)` call, which every SQL tool
reads as a function of a schema named `tpl`. The goal of a migration is a file
that a linter parses without a context, and that renders the same SQL, or SQL
that returns the same rows, as the Jinja file it replaces.

`Templates(path)` reads Jinja, the default engine, and
`Templates(path, engine="tpl")` reads `tpl`. Write the `tpl` templates in a
directory of their own, next to the old one, and point the database at it
once every template has moved. One `Templates` reads one
engine, so a directory moves as a whole. Never transpile a template from one
dialect to another. Write it in the production dialect, and keep dialect
differences inside macros.

## Before you start

List what the application has:

```console
$ uv run sqlakit macros                      # built-in macros
$ uv run sqlakit macros app.sql.macros       # and the application's own
$ grep -rn "filters=\|globals=\|Filter(" app  # Jinja filters and globals
$ grep -rln "{% include\|{% from\|{% import" app/sql
```

Every Jinja macro library (`{% from 'x.sql' import ... %}`), custom filter and
global needs a counterpart before the templates that use it can move. Write
those first, as `@sql_macro` functions in one module, and register the module
by path: `Templates("app/sql_tpl", engine="tpl", macros=["app.sql.macros"])`.

## Migrate one template

1. **Read everything it renders from:** the file, what it includes and
   imports, the filters and globals it uses, and every caller, to see the
   context it is handed.
2. **Sort it:** mechanical (only `{{ x }}`, `| inclause`, `{% if x %}`),
   dialect branches, or restructuring (`{% for %}`, choosing a table, a
   fragment included into an expression). Do the mechanical ones first.
3. **Write `x.sql` in the new directory,** under the same name, using the
   mapping below.
4. **Check it:** `sqlakit check`, or `Templates(new).check()`, reads every
   template of the new directory and refuses an unknown macro, a wrong
   argument count, a non-parameter where `:param` goes, a missing or circular
   include and an unclosed string, with the file and line.
5. **Compare it** with the old template, as below, on every dialect the
   application renders for.
6. **Switch the database** to the new directory once every template is there,
   `Templates(new, engine="tpl", macros=[...])` in place of the old path. Delete the old
   directory, and any filter or global nothing uses any more, and drop
   `filters=`, `globals=` and the `sql` extra from the application.

## The mapping

| Jinja | `tpl` |
|---|---|
| `{{ x }}` | `:x` |
| `{{ criteria.search }}`, `{{ d['key'] }}` | `:criteria.search`, `:d.key` (attribute, or key of a mapping) |
| `{{ Status.OPEN.value }}` from a global | pass `status=Status`, write `:status.OPEN.value` |
| `col IN {{ ids }}`, `col IN {{ ids \| inclause }}` | `col IN (:ids)` |
| `ARRAY[{{ ids \| inclause }}]`, a list outside `IN` | `ARRAY[tpl.each(:ids)]` |
| `{{ col \| identifier }}` | `tpl.identifier(:col, id, name, ...)`, with the names it may be |
| an ordering filter of the application, `ORDER BY {{ o \| sort }} NULLS LAST` | `ORDER BY tpl.order_by(:o, id, name, 'nulls_last')` |
| `{% if x %} AND cond {% endif %}` | `AND tpl.if_set(:x, cond)` |
| `{% if x %} a {% else %} b {% endif %}` in an expression | `tpl.if_set(:x, a, b)` |
| `{% if not x %} AND cond {% endif %}` | `AND tpl.unless_set(:x, cond)` |
| `{% if f and t %} AND d BETWEEN {{ f }} AND {{ t }} {% endif %}` | `AND tpl.between(d, :f, :t)`, `'[)'` for a half-open range |
| `ILIKE {{ '%' ~ q ~ '%' }}`, dialect branches for search | `tpl.icontains(col, :q)`, or `tpl.icontains(col, <expression>)` |
| `{% if dialect == ... %}` around `COLLATE`, `lower()` | `tpl.icollate(col[, 'und-ci-ai'])` |
| `JSON_BUILD_OBJECT` / `OBJECT_CONSTRUCT` branches | `tpl.json_object('k', v, ...)` |
| `ARRAY_AGG`, `STRING_AGG` / `LISTAGG` branches | `tpl.array_agg(v, order...)`, `tpl.string_agg(v, sep, order...)` |
| `= ANY(...)` / `ARRAY_CONTAINS` branches | `tpl.array_contains(array, value)` |
| any other `{% if dialect == ... %}` | an `@sql_macro` that names the difference and reads `ctx.dialect`; `tpl.on_dialect(postgresql = a, snowflake = b)` only for what has no name, such as a table in another database |
| `(VALUES {{ rows \| ... }})`, a small table | `FROM tpl.values(:rows) AS v`, columns `column1`, `column2`, ... |
| `{% include 'q.sql' %}`, a whole query | `FROM tpl.include('q.sql') AS q` |
| `{% include %}` of a fragment, `{% from 'lib.sql' import m %}` | an `@sql_macro` in Python |
| a plain filter, `{{ x \| f }}` | compute the value in Python and pass it |
| `Filter(func, bind=True)` | an `@sql_macro` taking `ctx: Context`, binding with `ctx.bind(value, "name")` |
| a global, `{{ settings.x }}`, `{% if feature_enabled(...) %}` | a parameter, or an `@sql_macro` that reads it |
| `{% for %}` over values | `IN (:list)`, `tpl.values(:rows)`, or an `@sql_macro` |
| `{% set %}` | compute it in the caller, or in a macro |
| `{# comment #}` | `-- comment` |
| an optional `UNION` branch or CTE | keep it, and make its condition `tpl.if_set(:flag, TRUE, FALSE)` |
| an optional `JOIN` | `LEFT JOIN ... ON ... AND tpl.if_set(:flag, TRUE, FALSE)`, or `EXISTS` |
| `FROM {% if x %} a {% else %} b {% endif %}` | `FROM tpl.if_set(:x, a, b)`, or `tpl.if_set(:x, tpl.include('a.sql'), tpl.include('b.sql'))` |

A macro of the application is written once and called like the built-in ones:

```python
from sqlakit.sql import Context, Param, Sql, sql_macro, tpl


@sql_macro
def owned_by(team_ids: Param, user_ids: Param) -> str:
    """Rows of any of the teams or users, and none when neither is given."""
    criteria = [
        f"{column} IN {param}"
        for column, param in (("team_id", team_ids), ("user_id", user_ids))
        if param.value
    ]
    return f"({' OR '.join(criteria)})" if criteria else "FALSE"


@sql_macro
def search(q: Param, *columns: Sql) -> str:
    """Rows where any of the columns holds the text, regardless of case."""
    if not q.value:
        return "TRUE"
    return " OR ".join(tpl.icontains(column, q) for column in columns)
```

`Param` is an argument written `:name`, with its `.value`, and `str(param)` is
the placeholder. `Sql` is an argument's text, macros inside it already expanded.
`Context`, first, gives `ctx.dialect`, `ctx.bind(value, "name")`,
`ctx.quote(name)` and `ctx.no_order`. `@sql_macro(optional=True)` reads a
parameter the call did not pass as `None`. A `Literal["'a'", "'b'"]` annotation
limits an argument to that SQL, checked when the file is read.

## What changes without a warning

- **Truthiness.** `if_set`, `unless_set` and `between` treat `None`, `False`,
  `""`, an empty list and a parameter not passed as missing, and `0` as there.
  Jinja's `{% if x %}` treats `0` as false. Look for numeric flags and ids.
- **An empty list in `IN (:ids)`** matches nothing. A Jinja template that
  wrapped the condition in `{% if ids %}` meant "no filter": write
  `tpl.if_set(:ids, col IN (:ids))`.
- **Search wildcards.** `'%' ~ q ~ '%'` in Jinja let `%` and `_` in the text
  act as wildcards. `icontains` matches them only as themselves.
- **Case in ordering.** `tpl.icollate` orders without regard to case on every
  database, so a test database that sorted `B` before `a` stops doing so.
- **Nothing is bound twice for one name.** An included template shares every
  parameter of the call, the way `include with context` does. One query
  included twice with different values needs two names.
- **`order_by` needs its columns.** Every name a request may sort by is listed
  after the parameter, `name = <expression>` for one that is not a column.
  Sort strings read any case convention: `createdAt.desc.nullsLast` works.
  A `NULLS LAST` after the call is refused: pass `'nulls_last'` instead.
- **Colons.** A POSIX class, `[[:punct:]]`, is left alone. Any other colon
  that is not a parameter is still written `\:`.

## Compare the old and the new

Render both with the same context and compare the SQL. `Templates.render`
takes the preparer of any dialect, so the production one can be checked
without connecting to it:

```python
from sqlalchemy.dialects import postgresql

from sqlakit.sql import Templates

old = Templates("app/sql")
new = Templates("app/sql_tpl", engine="tpl", macros=["app.sql.macros"])
context = {"dialect": "postgresql", "teams": ["red"], "q": None}
preparer = postgresql.dialect().identifier_preparer
old_sql, old_params = old.render("users/list.sql", context, preparer=preparer)
new_sql, new_params = new.render("users/list.sql", context, preparer=preparer)
print(old_sql, old_params, new_sql, new_params, sep="\n\n")
```

Jinja names parameters `teams__1` and macros keep `teams`, so compare the
shape, not the text. The values a `tpl` template returns are its whole
context and what its macros bound, such as `q__like__1` from the `icontains`
that `if_set` dropped. Only the names its SQL holds are bound when it runs.

For Snowflake, take the preparer of the Snowflake engine's dialect when
`snowflake-sqlalchemy` is installed. Without it, a
`sqlalchemy.engine.default.DefaultDialect()` with `name = "snowflake"` renders
the Snowflake branches, with SQLAlchemy's default quoting.

Then run the application's tests for every caller, with the context each one
builds, and for a query whose SQL changed shape, compare the rows it returns on
the test database. Where the production database is not the one tests run on,
parse the rendered production SQL with a linter for that dialect (`sqruff` with
`templater = placeholder` and `param_style = colon`).

## Done when

- `sqlakit check` passes, the database reads the new directory, and the old
  one is gone.
- Every caller passes the parameters the new file reads, flat or dotted, and no
  longer builds context only Jinja needed (`dialect` is added by `SQLAKit`).
- No macro library, filter or global is left that nothing calls.
- The rendered SQL for the production dialect parses with its linter.
- The tests for every caller pass, and the rows match where the SQL changed
  shape.
