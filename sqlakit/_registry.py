from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any

from ._base import _DatabaseRegistryMixin
from ._db import Database

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["Databases", "db"]


class Databases(_DatabaseRegistryMixin[Database], Database):
    """The databases an application talks to, and the default one among them.

    ```python
    from sqlakit import db

    db.configure(DB_URL)

    db.session  # the default database
    db["replica"].session  # another, if one was configured
    ```

    Use it when one database is enough and passing a handle around is not worth
    it. `Database(url)` is the alternative.
    """

    _database_class = Database

    @contextmanager
    def transactions(
        self,
        **arguments: Any,  # noqa: ANN401
    ) -> Iterator[None]:
        """Open a transaction on every database, not the default one alone.

        A single database needs `transaction(rollback=True)`. A test harness
        with several needs this:

        ```python
        with db.transactions(rollback=True):
            yield
        ```
        """
        with ExitStack() as stack:
            for alias in self.aliases:
                stack.enter_context(self[alias].transaction(**arguments))
            yield

    def dispose(self, *, close: bool = True) -> None:
        """Dispose of every configured database, not just the default one."""
        if self._built_its_own:
            super().dispose(close=close)
        elif self._default is not None:
            self._default.dispose(close=close)
        for db in self._aliased.values():
            db.dispose(close=close)


db = Databases()
"""The importable registry: one [`Databases`][sqlakit.Databases] for the process.

`db.configure(url)` fills it, and every module then reaches the same connections
by importing it. Reconfiguring is allowed until something connects, after which
it raises
[`DatabaseAlreadyConfiguredError`][sqlakit.DatabaseAlreadyConfiguredError]
and `dispose()` has to come first.
"""
