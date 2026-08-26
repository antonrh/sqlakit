"""What the tests of both APIs share."""

import inspect
from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture
def mirrors() -> Callable[[type, type], None]:
    """Hand over the check that the two APIs match."""
    return _mirrors


def _mirrors(sync: type, asynchronous: type) -> None:
    """Assert that the async class offers what the synchronous one does.

    The two are written by hand, so nothing else keeps them in step: a method
    added to one and forgotten in the other is a hole a reader falls into only
    under `asyncio`.
    """
    assert _surface(sync) == _surface(asynchronous)
    for member in sorted(_surface(sync)):
        assert _parameters(sync, member) == _parameters(asynchronous, member), member


def _surface(cls: type) -> set[str]:
    """Return what a class offers under its own name."""
    return {name for name in dir(cls) if not name.startswith("_")}


def _parameters(cls: type, member: str) -> list[tuple[str, Any, Any]] | None:
    """Return the parameters of a member, or None when it is not callable."""
    # Static: reading `Model.query` off the class would build a query instead.
    value = inspect.getattr_static(cls, member, None)
    value = getattr(value, "fget", value)  # a property
    value = getattr(value, "__func__", value)  # a classmethod
    if not callable(value):
        return None
    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError):  # pragma: no cover - a builtin
        return None
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in signature.parameters.values()
    ]
