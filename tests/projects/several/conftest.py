"""Two databases of the project's own, with no registry between them."""

from collections.abc import Iterator

import pytest
from app import OrderModel, ReportModel, orders_db, reporting_db

from sqlakit import Database


@pytest.fixture(scope="session")
def sqlakit_db() -> dict[str, Database]:
    return {"orders": orders_db, "reporting": reporting_db}


@pytest.fixture(scope="session")
def sqlakit_schema(sqlakit_db: dict[str, Database]) -> Iterator[None]:
    with OrderModel.provisioned_tables(), ReportModel.provisioned_tables():
        yield
