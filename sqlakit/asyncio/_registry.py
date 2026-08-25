from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlakit._base import _DatabaseRegistryMixin

from ._db import Database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["Databases", "db"]


class Databases(_DatabaseRegistryMixin[Database], Database):
    """The databases an application talks to, and the default one among them.

    The asyncio counterpart of [`sqlakit.Databases`][sqlakit.Databases].

    ```python
    from sqlakit.asyncio import db

    db.configure(DB_URL)

    db.session  # the default database
    db["replica"].session  # another, if one was configured
    ```
    """

    _database_class = Database

    @asynccontextmanager
    async def transactions(
        self,
        **arguments: Any,  # noqa: ANN401
    ) -> AsyncIterator[None]:
        """Open a transaction on every database, not the default one alone.

        What a test harness with more than one database needs, in the place a
        single database needs `transaction(rollback=True)`.
        """
        async with AsyncExitStack() as stack:
            for alias in self.aliases:
                await stack.enter_async_context(self[alias].transaction(**arguments))
            yield

    async def dispose(self, *, close: bool = True) -> None:
        """Dispose of every configured database, not just the default one."""
        if self.is_configured:
            await super().dispose(close=close)
        for db in self._aliased.values():
            await db.dispose(close=close)


db = Databases()
"""The database an application talks to. Configure it once, use it anywhere."""
