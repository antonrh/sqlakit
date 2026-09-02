"""What the plugin needs from a project: the base its models share."""

import pytest
from app import Model


@pytest.fixture(scope="session")
def sqlakit_base() -> type[Model]:
    return Model
