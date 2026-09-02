"""The fixtures every example test needs."""

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
