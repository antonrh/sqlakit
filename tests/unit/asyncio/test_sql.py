"""SQL kept in templates, awaited."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.orm.exc import NoResultFound

from sqlakit import (
    SQLNotConfiguredError,
    TemplateNotFoundError,
)
from sqlakit.asyncio import Database
from sqlakit.asyncio.orm import ModelMixin

TEMPLATES = {
    "notes/all.sql": """
        SELECT id, text FROM notes ORDER BY id
    """,
    "notes/by_text.sql": """
        SELECT id, text FROM notes WHERE text = {{ text }}
    """,
    "notes/rename.sql": """
        UPDATE notes SET text = {{ to }} WHERE text = {{ from_ }}
    """,
}


class Base(ModelMixin, DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]


class NoteRow(BaseModel):
    id: int
    text: str


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    for name, source in TEMPLATES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


@pytest.fixture
async def db(templates: Path) -> AsyncIterator[Database]:
    db = Database(
        "sqlite+aiosqlite://",
        engine_args={"poolclass": sa.StaticPool},
        templates=templates,
    )
    Base.set_db(db)
    async with db.transaction() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.transaction():
        for index, text in enumerate("abcde"):
            await Note(id=index + 1, text=text).save()
    yield db
    await db.dispose()


@pytest.mark.anyio
async def test_a_template_reads_rows(db: Database) -> None:
    async with db.connect():
        rows = await db.sql("notes/all.sql").all()

        assert [row.text for row in rows] == list("abcde")


@pytest.mark.anyio
async def test_rows_come_back_as_the_type_asked_for(db: Database) -> None:
    async with db.connect():
        query = db.sql("notes/by_text.sql", text="c").typed(NoteRow)

        assert await query.one() == NoteRow(id=3, text="c")
        assert await query.first() == NoteRow(id=3, text="c")
        assert await query.all() == [NoteRow(id=3, text="c")]


@pytest.mark.anyio
async def test_scalars_and_the_rows_that_are_not_there(db: Database) -> None:
    async with db.connect():
        assert await db.sql("notes/all.sql").scalars().all() == [1, 2, 3, 4, 5]
        assert await db.sql("notes/by_text.sql", text="zz").one_or_none() is None

        with pytest.raises(NoResultFound):
            await db.sql("notes/by_text.sql", text="zz").one()


@pytest.mark.anyio
async def test_a_statement_built_with_sqlalchemy_is_read_the_same_way(
    db: Database,
) -> None:
    async with db.connect():
        statement = sa.select(sa.literal(7))

        assert await db.sql.from_statement(statement).typed(int).one() == 7


@pytest.mark.anyio
async def test_chunks_walk_the_whole_result(db: Database) -> None:
    async with db.connect():
        batches = [
            [row.text for row in batch]
            async for batch in db.sql("notes/all.sql").chunks(2)
        ]
        scalars = [
            list(batch) async for batch in db.sql("notes/all.sql").scalars().chunks(4)
        ]

        assert batches == [["a", "b"], ["c", "d"], ["e"]]
        assert scalars == [[1, 2, 3, 4], [5]]


@pytest.mark.anyio
async def test_a_template_that_writes_says_how_many_rows_it_touched(
    db: Database,
) -> None:
    async with db.connect():
        touched = await db.sql("notes/rename.sql", to="z", from_="a").execute()

        assert touched == 1


@pytest.mark.anyio
async def test_a_writing_template_commits_when_no_transaction_is_open(
    db: Database,
) -> None:
    # With no transaction to leave the commit to, `execute()` commits for itself.
    async with db.connect():
        await db.sql("notes/rename.sql", to="z", from_="a").execute()

    async with db.connect():
        notes = await db.sql("notes/by_text.sql", text="z").all()

        assert notes != []


@pytest.mark.anyio
async def test_a_template_maps_onto_the_model(db: Database) -> None:
    async with db.connect():
        notes = await Note.query.from_sql("notes/by_text.sql", text="b").all()

        assert [note.id for note in notes] == [2]
        assert all(isinstance(note, Note) for note in notes)


@pytest.mark.anyio
async def test_sql_written_out_here_needs_no_templates() -> None:
    db = Database("sqlite+aiosqlite://", engine_args={"poolclass": sa.StaticPool})

    async with db.connect():
        assert await db.sql.from_string("SELECT {{ n }}", n=7).scalars().one() == 7

        with pytest.raises(SQLNotConfiguredError):
            await db.sql("notes/all.sql").all()

    await db.dispose()


@pytest.mark.anyio
async def test_a_template_nobody_has_says_where_it_looked(db: Database) -> None:
    async with db.connect():
        with pytest.raises(TemplateNotFoundError):
            await db.sql("notes/nothing.sql").all()


@pytest.mark.anyio
async def test_the_templates_are_reachable_and_compile(db: Database) -> None:
    db.sql.check()

    assert repr(db.sql).startswith("SQL(")
    assert db.sql.templates.paths
    assert "/* notes/all.sql */" in str(db.sql("notes/all.sql").statement)
