# Changelog

## 0.10.0

### Added

- `Database(url, alias="warehouse")` names a database built by hand. The name
  is what a recorded statement carries, so two databases in one recording, or
  two recordings under one label, can be told apart. A registry still names the
  databases it holds after the aliases they are registered under.
- `db.recording(using=...)` on a registry records only the databases named, by
  alias or in person, as `assert_queries` already took them.

### Changed

- The debug server's page names the databases a recording covered: on the row,
  beside the label of the open recording, beside each statement, and beside
  each table in the filters. A page with one database says nothing about
  databases at all.
- A row on that page says how many of its statements were slow, and its time is
  amber over 10 ms and red over 100 ms. Slow statements were marked only inside
  the recording that was open.

### Fixed

- The page finds the server again after it is restarted. An error on the stream
  set the page to disconnected and nothing cleared it, so a server that came
  back was never picked up.

## 0.9.1

### Fixed

- A registry answers for a database registered as `default`. Registering one
  used to raise, and `Model.register_db(db)` without an alias pointed the
  models at the database while leaving the registry without one, so the two
  disagreed: `db["default"]` was the registry itself, `transactions()` failed
  on it with an `AttributeError`, and the `pytest` plugin created the tables of
  one alias out of several and rolled back one database out of several.
- A registry with no database of its own raises `DatabaseNotConfiguredError`
  rather than failing on state it never built, and says to reach the default as
  `db["default"]` when that is where it was registered.

## 0.9.0

### Added

- A debug server, for seeing what a block ran while it is running.
  `sqlakit debugserver` serves a page, and
  `db.recording("GET /users", debugserver=("localhost", 5555))` sends the
  recording to it when the block ends. Sending happens on a thread of its own,
  so the block that recorded is not waiting on it, and a server that is not
  there is not the application's problem.
- The page shows the recordings as they arrive, and for the one open, every
  statement in order: what it took, its parameters, which of them ran more than
  once, and the lines of yours that ran it. `table:users ms:>50` reads as a
  search, over the label, the SQL, the tables, the database and the trace, and
  over what a recording counted. It filters by application and tag, sorts, and
  lays a statement out in full or as it was sent.
- `pytest --sqlakit-report` writes that same page for a test run, as one file
  that opens without a server. Every recording is labelled by the test and
  carries the file it lives in. `--sqlakit-report=PATH` names the file, and the
  flag on its own names it after the clock, so a run keeps the one before it.
- `sqlakit_skip_queries_from` names the files whose queries stay out of a
  report, a factory or a helper among them, so what it shows is the code under
  test rather than the rows a fixture wrote.
- `recording(..., skip_queries_from=...)`, the same for one block.
- `DebugServer` says where recordings go, and under which application and tags,
  for a project that sends from a web process and a worker at once.
- `Statement.dialect`, what ran the statement, as SQLAlchemy names it.

### Changed

- The frames a statement remembers with `stacks=True` are the ones you wrote.
  Installed packages go, the test runner and the event loop among them, and so
  does the code SQLAlchemy generates, whose line numbers lead nowhere. A caller
  with no frames of its own, a library calling from `site-packages`, gets them
  back rather than nothing.

### Documentation

- The debugging page has a section on the debug server: what the command
  prints, what to send it, and the page itself in a screenshot.
- The site carries the library's mark and colours rather than the theme's
  defaults.

## 0.8.1

### Fixed

- The `pytest` plugin opens the test's transaction before the fixtures the test
  asked for. It opened it after them, so a fixture that wrote rows wrote them
  outside the transaction: with the model layer that raised
  `MissingSessionError`, and a fixture that opened a session of its own left
  rows behind for the tests that followed. A fixture of a wider scope, one that
  seeds a module or a class, still wraps the test.

### Documentation

- The hand-written conftest inserts the fixture rather than appending it, and
  says what the order means.
- A project with no model layer has a section of its own, on `sqlakit_db` and
  `sqlakit_metadata`.

## 0.8.0

### Added

- A `pytest` plugin, installed with the library and turned on with
  `sqlakit = true`. It registers the `db` marker, creates the schema once for
  the session, and runs every marked test in a transaction that rolls back.
  Tests without the marker connect to nothing.
- `@pytest.mark.db(using=...)` names the databases a test opens, by alias or by
  database. Without it a test opens every one, which for most projects is the
  one they have.
- Five fixtures a project overrides: `sqlakit_base`, the base its models
  inherit, which is also where the database comes from; `sqlakit_db`, the
  database itself; `sqlakit_schema`, how the schema is created, for a suite
  running migrations against a server of its own; `sqlakit_seed`, the rows
  every test starts from; and `sqlakit_metadata`, for a project with no model
  layer.

### Documentation

- The testing page opens with the plugin, and keeps the hand-written conftest
  after it. It says what `autoflush` costs, when `save()` is needed, and how to
  start a server with `pytest-docker`.

## 0.7.4

### Documentation

- The testing page uses one conftest shape for the synchronous and the
  `asyncio` sides, which differ only in `with` against `async with`. The
  fixtures are named for what they give, `_db_schema` and `_db_transaction`,
  and one copy of the old `_db_marker` had lost its marker check, so it opened
  a transaction for every test in the suite.

## 0.7.3

### Changed

- A plain column of another table in `__orderable__` is joined through a
  relationship of the model that reaches that table, condition and all. It was
  joined on the foreign key between the tables, which a view carrying no key
  does not have, and which cannot carry a discriminator. The key is still used
  when no relationship reaches the table.

### Documentation

- The reference says that `autoflush` stays at `SQLAlchemy`'s `True`, what a
  block that alternates changes and queries pays for it, and what turning it
  off costs.
- The models page says when `save()` is needed: a new instance needs it, a row
  the block read does not, and in a loop that changes rows it is a write per
  row.

## 0.7.2

### Fixed

- A template value may be named `template` or `source`. Every keyword is a
  value, and those two collided with the argument holding the file name, which
  is positional now on `sql()`, `from_file`, `from_string` and `from_sql`.

## 0.7.1

### Fixed

- Two ordering fields that join one table on different conditions raise
  `ConflictingJoinError`. A statement joins a table once, so the second
  condition was dropped and the field ordered by the first one's rows. Give
  each field an alias, and each gets a join of its own.

## 0.7.0

### Changed

- Ordering by a field in another table joins it with an outer join. It was an
  inner one, so a row with nothing on the other side disappeared from the
  results and `page.total` counted it out. `OrderBy(..., outer=False)` keeps
  the inner join, and `nulls` says where the rows with no match go.
- A model that looks an alias up nowhere raises `MissingRegistryError`, naming
  the model and the alias. It raised `MissingDefaultDatabaseError`, whose
  message is about a configuration mapping needing a `default` key.

### Added

- A plain column of another table can be named in `__orderable__` directly,
  without an `OrderBy`. Its table is joined on the key between them, once
  however many fields name it. Naming one used to build a statement whose
  `FROM` lacked the table.
- `register_db(db)` without an alias points the model at that database, the
  same as `set_db(db)`.

## 0.6.0

### Changed

- `Page.total` is typed by how the page was read. `page(limit=20)` returns a
  `Page[User]` whose `total` is an `int`, and `page(total=False)` returns a
  `Page[User, None]`. Nothing changes at run time, but a type checker now
  refuses a page read without counting where a counted one is expected, and
  one without PEP 696 type parameter defaults refuses the library itself.
  `mypy` has them from 1.12.

### Added

- `UncountedPage[User]`, the name for a page read with `total=False`.
- `orderable_columns(model)`, every mapped column of a model. What an
  `__orderable__` that adds to the columns rather than replacing them starts
  from, since calling `orderable` there reads the method that is running.

## 0.5.1

### Added

- `Model.register_db(db, alias=...)` puts a database under an alias in a
  registry belonging to that class, so a set of models can have several
  databases without configuring the importable registry. `Model.dbs` reaches
  it, and a model under that class registers into the same one.
- `Databases.register(alias, db)` does the same on a registry directly, for a
  shard that only exists once the application runs. The alias has to be free,
  `AliasInUseError` otherwise, and cannot be `default`, `DefaultAliasError`.

### Documentation

- `__dbs__`, where a model looks an alias up, is written down, along with the
  registry of its own that `register_db` builds.
- `using()` says that a model living somewhere else still needs a block open on
  its own database. The page said both halves, two sections apart.

## 0.5.0

### Changed

- `order_by` now matches a field name whichever case convention it arrives in,
  so an API sending `userName` orders by the `user_name` the model declares. A
  model that offers both spellings is matched exactly, and a name that could
  mean either is refused.
- A name in `ignore_case` that the model does not offer now raises
  `UnknownOrderFieldError`. It used to be ignored, which left the column
  comparing with regard to case and said nothing.

### Added

- `order_by` takes `nulls`, `first` or `last`, saying where the rows with no
  value go. Without it the database decides, and `PostgreSQL` is the mirror of
  `SQLite` and `MySQL`. It fills in only what neither the sort string nor the
  model said.
- `TemplatesLike` is a public type, in `sqlakit.types` beside `EngineArgs` and
  `SessionArgs`. It names what `Database(templates=...)` takes.
- `InvalidNullsError`, raised when `nulls` is neither `first` nor `last`.

### Fixed

- Ordering with `nulls_first` or `nulls_last` no longer raises a syntax error
  on `MySQL` and `MariaDB`, which have neither. The placement is compiled for
  the dialect that runs the query, so all of them put the rows in the same
  places.

Every released version, and what changed in it. The same text is on the
[releases page](https://github.com/antonrh/sqlakit/releases).

`SQLAKit` is on `0.x`, so a minor version is where something breaks and a patch
version is where nothing does.

## 0.4.0 (2026-08-31)

### Changed

- `order_by` takes `ignore_case` in place of `ci_fields`, which named the field
  twice. Pass `True` for the fields of the call, or the names it applies to
  when the sort arrived as a list from a request.
- Ordering without regard to case asks the database how to compare, instead of
  always ordering by `lower()`. `SQLite` orders by `COLLATE NOCASE`, and a
  dialect with no collation named for it orders by `lower()`, which every SQL
  database has. The dialect is read when the query runs, so the same model
  orders on `SQLite` under test and on the server it ships to.

### Added

- `CASE_INSENSITIVE_COLLATIONS`, the collation `ignore_case` orders by, per
  dialect. Name one to order by an index, or to decide what happens to accents.

## 0.3.1 (2026-08-29)

### Changed

- The `README` shows Active Record after the registry and multiple databases,
  with a smaller example.

## 0.3.0 (2026-08-27)

### Changed

- The async API needs the `sqlakit[asyncio]` extra, which brings
  `sqlalchemy[asyncio]` with it.

## 0.2.0 (2026-08-27)

### Changed

- `session_factory()` takes a connection from the pool when the session first
  needs one, not when the block starts, the way `sessionmaker()` works.
- A `transaction()` inside `connect()` or `session_factory()` runs on the
  connection that block already opened, instead of taking a second one.
  `transaction(join_nested=False)` and a block inside `autocommit()` still get
  their own connection.
- The minimum `SQLAlchemy` version is 2.0.22, down from 2.0.43.

### Fixed

- `cursor_page()` builds the ordering once per page instead of three times.

## 0.1.0 (2026-08-26)

First release.
