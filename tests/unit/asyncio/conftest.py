"""Fixtures every test in this directory gets."""

import threading
from collections.abc import AsyncIterator

import pytest


def _workers() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if "_connection_worker_thread" in t.name}


@pytest.fixture(autouse=True)
async def _join_aiosqlite_workers() -> AsyncIterator[None]:
    """Join the aiosqlite threads a test started, while its loop is still open.

    A connection that fails to open leaves its thread running, and the thread
    raises out of itself once the loop closes under it.
    """
    running = _workers()
    yield
    for thread in _workers() - running:
        thread.join(5)
