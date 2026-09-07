"""Two bases: the registry is named here, and the schema covers both."""

from collections.abc import Iterator

import pytest
from app import Model, WarehouseModel, db

from sqlakit import Databases


@pytest.fixture(scope="session")
def sqlakit_db() -> Databases:
    return db


@pytest.fixture(scope="session")
def sqlakit_schema(sqlakit_db: Databases) -> Iterator[None]:
    with Model.provisioned_tables(), WarehouseModel.provisioned_tables():
        yield
