"""A project that builds its databases itself and registers them.

The other one under `plugin` configures the importable registry from settings.
This one hands over databases it built, the default among them.
"""

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database, EngineArgs
from sqlakit.orm import ModelMixin

ARGS: EngineArgs = {"poolclass": sa.StaticPool}


class Model(ModelMixin, DeclarativeBase):
    pass


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]


class Event(Model):
    __tablename__ = "events"
    __db__ = "warehouse"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    what: Mapped[str]


Model.register_db(Database("sqlite://", engine_args=ARGS))
Model.register_db(Database("sqlite://", engine_args=ARGS), alias="warehouse")
