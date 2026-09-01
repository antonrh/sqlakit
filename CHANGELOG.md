# Changelog

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
