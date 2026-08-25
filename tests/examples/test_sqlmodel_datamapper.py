"""`examples/sqlmodel_datamapper.py`, run against a database."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlmodel import SQLModel

from examples.sqlmodel_datamapper import Author, BookRepository
from sqlakit import Database, UnknownOrderFieldError


@pytest.fixture
def repository() -> Iterator[BookRepository]:
    """The example's repository, on a database of its own, holding three books."""
    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    with db.transaction() as conn:
        SQLModel.metadata.create_all(conn)

    with db.transaction():
        repository = BookRepository(db)
        author = Author(name="ada")
        db.session.add(author)
        db.session.flush()
        for index, title in enumerate(("alpha", "beta", "gamma")):
            book = repository.add(title, author)
            book.published_at = datetime(2026, 1, index + 1, tzinfo=UTC)
        yield repository


@pytest.fixture
def author(repository: BookRepository) -> Author:
    """The author those books were written by."""
    return repository.db.session.scalars(sa.select(Author)).one()


def test_a_repository_reads_sqlmodel_classes(
    repository: BookRepository, author: Author
) -> None:
    found = repository.by_title("beta")

    assert found is not None
    assert found.title == "beta"
    assert [book.title for book in repository.by_author(author)] == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_a_repository_pages_both_ways(repository: BookRepository) -> None:
    page = repository.page(sort="title", limit=2)
    feed = repository.feed(limit=2)
    rest = repository.feed(limit=2, cursor=feed.next_cursor)

    assert [book.title for book in page.items] == ["alpha", "beta"]
    assert (page.total, page.has_next) == (3, True)
    assert [book.title for book in feed.items] == ["gamma", "beta"]
    assert [book.title for book in rest.items] == ["alpha"]


def test_a_repository_refuses_a_sort_the_model_does_not_offer(
    repository: BookRepository,
) -> None:
    with pytest.raises(UnknownOrderFieldError):
        repository.page(sort="secret", limit=2)
