"""A project with a base for each database, on one registry."""

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Databases, EngineArgs
from sqlakit.orm import ModelMixin

ARGS: EngineArgs = {"poolclass": sa.StaticPool}

db = Databases()
db.configure(
    {
        "default": {"url": "sqlite://", "engine_args": ARGS},
        "warehouse": {"url": "sqlite://", "engine_args": ARGS},
    }
)


class Model(ModelMixin, DeclarativeBase):
    pass


class WarehouseModel(ModelMixin, DeclarativeBase):
    __db__ = "warehouse"


Model.__dbs__ = db
WarehouseModel.__dbs__ = db


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]


class Event(WarehouseModel):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    what: Mapped[str]
