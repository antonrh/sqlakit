"""A project the `sqlakit` pytest plugin tests.

The plugin ships with the library. A project turns it on and says which models
it has, and every test marked `db` then runs in a transaction that rolls back.
The files next to this one are the whole setup: `pytest.ini`, `conftest.py`,
and the tests.
"""

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database
from sqlakit.orm import ModelMixin

db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})


class Model(ModelMixin, DeclarativeBase):
    __db__ = db


class User(Model):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
    team: Mapped[str] = mapped_column(default="red")


def rename(user_id: int, name: str) -> None:
    """Rename a user, on the block the test opened."""
    User.query.get_one(user_id).update({"name": name}).save()
