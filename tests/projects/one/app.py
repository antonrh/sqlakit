"""A project with one database, registered rather than configured."""

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database, EngineArgs
from sqlakit.orm import ModelMixin


class Model(ModelMixin, DeclarativeBase):
    pass


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]


Model.register_db(
    Database("sqlite://", engine_args=EngineArgs(poolclass=sa.StaticPool))
)
