import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sqlakit import Database, EngineArgs
from sqlakit.orm import ModelMixin

ARGS: EngineArgs = {"poolclass": sa.StaticPool}

orders_db = Database("sqlite://", engine_args=ARGS, alias="orders")
reporting_db = Database("sqlite://", engine_args=ARGS, alias="reporting")


class OrderModel(ModelMixin, DeclarativeBase):
    pass


class ReportModel(ModelMixin, DeclarativeBase):
    pass


OrderModel.set_db(orders_db)
ReportModel.set_db(reporting_db)


class Order(OrderModel):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    what: Mapped[str]


class Report(ReportModel):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
