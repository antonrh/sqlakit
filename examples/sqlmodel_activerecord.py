"""SQLModel in the Active Record style: the model saves and reads itself.

`ModelMixin` puts `save()`, `delete()` and `Model.query` on a SQLModel class.
Mix it in after `SQLModel`, which is the order pydantic asks for. See
``sqlmodel_datamapper.py`` for the same shape of application with repositories
instead.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar, Self

from sqlmodel import Field, SQLModel, col

from sqlakit import CursorPage, Page
from sqlakit.asyncio.orm import ModelMixin, Query


class Base(SQLModel, ModelMixin):
    """Carry SQLModel and the model layer together.

    `Base.set_db(...)` at startup points every model under it at one db.
    """


class Writer(Base, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class NovelQuery(Query["Novel"]):
    def by_writer(self, writer: Writer) -> Self:
        return self.where(col(Novel.writer_id) == writer.id)

    def published(self) -> Self:
        return self.where(col(Novel.published_at) <= datetime.now(UTC))


class Novel(Base, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    writer_id: int = Field(foreign_key="writer.id")
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # A pydantic model wants every class attribute annotated, and a `ClassVar`
    # is how you say this one is not a field.
    query: ClassVar[NovelQuery] = NovelQuery.as_descriptor()

    # Every mapped column is sortable unless a model narrows it. These are the
    # names the API may send; `writer_id` and `id` are not among them.
    __orderable__ = ("title", "published_at")


async def write_a_novel(title: str, writer: Writer) -> Novel:
    return await Novel(title=title, writer_id=writer.id or 0).save()


async def novels_by(writer: Writer) -> Sequence[Novel]:
    return await Novel.query.by_writer(writer).order_by("title").all()


async def page_of_novels(*, sort: str, limit: int, offset: int = 0) -> Page[Novel]:
    return await Novel.query.published().order_by(sort).page(limit=limit, offset=offset)


async def novel_feed(*, limit: int, cursor: str | None = None) -> CursorPage[Novel]:
    return await Novel.query.order_by("published_at.desc").cursor_page(
        limit=limit, cursor=cursor
    )
