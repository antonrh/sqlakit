import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import EngineArgs, db
from sqlakit.orm import ModelMixin

ARGS: EngineArgs = {"poolclass": sa.StaticPool}
db.configure(
    {
        "default": {"url": "sqlite://", "engine_args": ARGS},
        "warehouse": {"url": "sqlite://", "engine_args": ARGS},
        "audit": {"url": "sqlite://", "engine_args": ARGS},
    }
)


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
