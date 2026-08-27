# Changelog

## 0.2.0

### Changed

- `session_factory()` now takes a connection from the pool when the session
  first needs one, not when the block starts, the way `sessionmaker()` works.
- A `transaction()` inside `connect()` or `session_factory()` now runs on the
  connection that block already opened, instead of taking a second one.
  `transaction(join_nested=False)` and a block inside `autocommit()` still get
  their own connection.
- The minimum `SQLAlchemy` version is now 2.0.22, down from 2.0.43.

### Fixed

- `cursor_page()` no longer builds the same ordering three times per page.

## 0.1.0

The first release.
