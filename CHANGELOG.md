# Changelog

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
