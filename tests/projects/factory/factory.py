"""A factory of rows, the kind a suite writes to set the scene."""

from __future__ import annotations

from typing import Any


def make(model: type[Any], **values: Any) -> Any:
    """Write a row, the way a test factory does."""
    return model(**values).save()
