"""SQLModel in the data mapper style: models that know nothing, repositories that do.

SQLModel classes are SQLAlchemy models, so the query works on them as it works
on any other. Nothing here inherits from SQLAKit — the repository holds the
db and hands out queries, and the models stay plain. See
``sqlmodel_activerecord.py`` for the same models the other way round.
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlmodel import Field, SQLModel, col

from sqlakit import CursorPage, Database, Page
from sqlakit.orm import Query


class Author(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str


class Book(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    author_id: int = Field(foreign_key="author.id")
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Every mapped column is sortable unless a model narrows it. These are the
    # names the API may send; a classmethod goes here instead when a field is
    # not a plain column — see the docs for `__orderable__`.
    __orderable__ = ("title", "published_at")


class BookRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def query(self) -> Query[Book]:
        return Query(Book, self.db)

    def add(self, title: str, author: Author) -> Book:
        book = Book(title=title, author_id=author.id or 0)
        self.db.session.add(book)
        self.db.session.flush()
        return book

    def by_title(self, title: str) -> Book | None:
        return self.query.where(col(Book.title) == title).first()

    def by_author(self, author: Author) -> Sequence[Book]:
        return (
            self.query.where(col(Book.author_id) == author.id).order_by("title").all()
        )

    def page(self, *, sort: str, limit: int, offset: int = 0) -> Page[Book]:
        return self.query.order_by(sort).page(limit=limit, offset=offset)

    def feed(self, *, limit: int, cursor: str | None = None) -> CursorPage[Book]:
        return self.query.order_by("published_at.desc").cursor_page(
            limit=limit, cursor=cursor
        )
