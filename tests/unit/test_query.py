import base64
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Self

import pytest
import sqlalchemy as sa
import sqlalchemy.event
from dirty_equals import IsStr
from sqlalchemy.orm import (
    DeclarativeBase,
    InstrumentedAttribute,
    Mapped,
    aliased,
    joinedload,
    mapped_column,
    relationship,
)
from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound

import sqlakit.asyncio.orm
import sqlakit.orm
from sqlakit import (
    BulkQueryError,
    Database,
    InstanceNotFoundError,
    InvalidCursorError,
    InvalidOrderFieldError,
    KeyLookupError,
    MultipleInstancesFoundError,
    OrderBy,
    PageItemsMismatchError,
    RawStatementError,
    UncomparableOrderingError,
    UnknownOrderFieldError,
    UnorderedPageError,
)
from sqlakit._query import _is_joined, _join_identity, _payload
from sqlakit.orm import ModelMixin, Query, SoftDeletes
from sqlakit.testing import assert_queries


class Base(ModelMixin, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    team: Mapped[str]


@pytest.fixture
def db() -> Database:
    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})
    Base.set_db(db)
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    with db.transaction():
        for index, name in enumerate("abcde"):
            User(id=index + 1, name=name, team="red" if index % 2 else "blue").save()
    return db


def test_reading(db: Database) -> None:
    with db.connect():
        assert [user.name for user in User.query.order_by(User.id).all()] == list(
            "abcde"
        )
        first = User.query.order_by(User.id).first()
        assert first is not None
        assert first.name == "a"
        assert User.query.where(User.name == "c").one().id == 3
        assert User.query.where(User.name == "zz").one_or_none() is None
        assert User.query.count() == 5
        assert User.query.where(User.team == "red").count() == 2
        assert User.query.where(User.name == "a").exists() is True
        assert User.query.where(User.name == "zz").exists() is False

        with pytest.raises(NoResultFound):
            User.query.where(User.name == "zz").one()


def test_the_database_builds_a_query_over_any_mapped_class(db: Database) -> None:
    with db.connect():
        assert db.query(User).order_by("name").count() == 5
        assert [user.name for user in db.query(User).order_by("name").all()] == list(
            "abcde"
        )


def test_a_query_can_be_branched_from(db: Database) -> None:
    with db.connect():
        red = User.query.where(User.team == "red")

        assert red.count() == 2
        assert red.where(User.name == "b").count() == 1
        assert red.count() == 2  # the branch left it alone


def test_page(db: Database) -> None:
    with db.connect():
        page = User.query.order_by(User.id).page(limit=2, offset=2)

        assert [user.name for user in page.items] == ["c", "d"]
        assert page.total == 5
        assert page.has_next is True

        last = User.query.order_by(User.id).page(limit=2, offset=4)

        assert [user.name for user in last.items] == ["e"]
        assert last.has_next is False


def test_page_past_the_end(db: Database) -> None:
    with db.connect():
        page = User.query.order_by(User.id).page(limit=2, offset=99)

        assert page.items == []
        assert page.total == 5
        assert page.has_next is False


def test_page_without_the_count(db: Database) -> None:
    with db.connect(), assert_queries(1, using=db):
        page = User.query.order_by(User.id).page(limit=2, offset=2, total=False)

        assert [user.name for user in page.items] == ["c", "d"]
        assert page.total is None
        assert page.has_next is True

    with db.connect():
        last = User.query.order_by(User.id).page(limit=2, offset=4, total=False)

        assert [user.name for user in last.items] == ["e"]
        assert last.total is None
        assert last.has_next is False


@pytest.mark.parametrize(
    ("order", "first", "second"),
    [
        (lambda: User.id, ["a", "b"], ["c", "d"]),
        (lambda: User.id.desc(), ["e", "d"], ["c", "b"]),
        (lambda: User.team, ["a", "c"], ["e", "b"]),
    ],
)
def test_cursor_page_walks_the_ordering(
    db: Database,
    order: Callable[[], Any],
    first: list[str],
    second: list[str],
) -> None:
    with db.connect():
        page = User.query.order_by(order()).cursor_page(limit=2)

        assert [user.name for user in page.items] == first
        assert page.has_next is True

        following = User.query.order_by(order()).cursor_page(
            limit=2, cursor=page.next_cursor
        )

        assert [user.name for user in following.items] == second


def test_cursor_page_with_mixed_directions(db: Database) -> None:
    # Ordering that no row comparison can express: the pages still line up.
    order = (User.team.desc(), User.id.asc())
    with db.connect():
        seen: list[str] = []
        cursor = None
        while True:
            page = User.query.order_by(*order).cursor_page(limit=2, cursor=cursor)
            seen += [user.name for user in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break

        expected = [user.name for user in User.query.order_by(*order).all()]
        assert seen == expected


def test_cursor_page_reaches_the_end(db: Database) -> None:
    with db.connect():
        page = User.query.order_by(User.id).cursor_page(limit=10)

        assert len(page.items) == 5
        assert page.next_cursor is None
        assert page.has_next is False


def test_cursor_page_breaks_ties_by_primary_key(db: Database) -> None:
    # Every row shares a team, so the ordering alone cannot separate them.
    with db.transaction():
        for user in User.query.all():
            user.team = "red"

    with db.connect():
        seen: list[int] = []
        cursor = None
        while True:
            page = User.query.order_by(User.team).cursor_page(limit=2, cursor=cursor)
            seen += [user.id for user in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break

        assert seen == [1, 2, 3, 4, 5]


def test_a_new_ordering_works_the_keyset_out_again(db: Database) -> None:
    # A page works the ordering out once and keeps it. A query built from that
    # one orders differently, so it must not inherit the answer.
    with db.connect():
        base = User.query.order_by(User.id)
        base.cursor_page(limit=2)

        assert base.order_by(User.name)._keyset()[1] != base._keyset()[1]


def test_a_cursor_from_elsewhere_is_refused(db: Database) -> None:
    with db.connect():
        with pytest.raises(InvalidCursorError):
            User.query.order_by(User.id).cursor_page(limit=2, cursor="nonsense")

        page = User.query.order_by(User.id).cursor_page(limit=2)

        with pytest.raises(InvalidCursorError):
            # Made for one column, read back against two.
            User.query.order_by(User.team, User.name).cursor_page(
                limit=2, cursor=page.next_cursor
            )

        with pytest.raises(InvalidCursorError):
            # Same arity and type, but a different ordering.
            User.query.order_by(User.name).cursor_page(limit=2, cursor=page.next_cursor)

        with pytest.raises(InvalidCursorError):
            # The same column, ordered the other way.
            User.query.order_by(User.id.desc()).cursor_page(
                limit=2, cursor=page.next_cursor
            )


def test_cursor_page_reads_backwards(db: Database) -> None:
    with db.connect():
        first = User.query.order_by(User.id).cursor_page(limit=2)
        second = User.query.order_by(User.id).cursor_page(
            limit=2, cursor=first.next_cursor
        )

        assert [user.name for user in second.items] == ["c", "d"]
        assert second.has_previous is True

        back = User.query.order_by(User.id).cursor_page(
            limit=2, cursor=second.previous_cursor
        )

        assert [user.name for user in back.items] == ["a", "b"]
        assert back.has_next is True
        # Nothing in front of the first page, so no cursor to go back with.
        assert back.previous_cursor is None
        assert back.has_previous is False

        assert first.previous_cursor is None


@pytest.mark.parametrize(
    "order",
    [
        (lambda: (User.id,)),
        (lambda: (User.id.desc(),)),
        (lambda: (User.team.desc(), User.id.asc())),
    ],
)
def test_a_walk_back_retraces_the_walk_out(
    db: Database, order: Callable[[], tuple[Any, ...]]
) -> None:
    with db.connect():
        forwards: list[list[str]] = []
        page = User.query.order_by(*order()).cursor_page(limit=2)
        while True:
            forwards.append([user.name for user in page.items])
            if page.next_cursor is None:
                break
            page = User.query.order_by(*order()).cursor_page(
                limit=2, cursor=page.next_cursor
            )

        backwards = [forwards[-1]]
        while page.previous_cursor is not None:
            page = User.query.order_by(*order()).cursor_page(
                limit=2, cursor=page.previous_cursor
            )
            backwards.insert(0, [user.name for user in page.items])

        expected = [user.name for user in User.query.order_by(*order()).all()]

        assert [name for names in forwards for name in names] == expected
        assert backwards == forwards


def test_a_cursor_carries_the_way_it_goes(db: Database) -> None:
    # One parameter, whichever button the reader pressed: the direction is
    # part of the cursor rather than of the call.
    with db.connect():
        page = User.query.order_by(User.id).cursor_page(limit=2)
        second = User.query.order_by(User.id).cursor_page(
            limit=2, cursor=page.next_cursor
        )

    assert page.next_cursor is not None
    assert second.previous_cursor is not None
    ordering = IsStr(regex="[0-9a-f]{8}")
    assert _payload(page.next_cursor) == {"v": [2], "b": False, "o": ordering}
    assert _payload(second.previous_cursor) == {"v": [3], "b": True, "o": ordering}


def test_a_page_whose_rows_are_gone_is_empty(db: Database) -> None:
    with db.connect():
        first = User.query.order_by(User.id).cursor_page(limit=2)
        second = User.query.order_by(User.id).cursor_page(
            limit=2, cursor=first.next_cursor
        )

    with db.transaction():
        User.query.where(User.id <= 2).delete()

    with db.connect():
        # The rows this cursor pointed back at are no longer there.
        nothing = User.query.order_by(User.id).cursor_page(
            limit=2, cursor=second.previous_cursor
        )

        assert nothing.items == []
        assert nothing.next_cursor is None
        assert nothing.previous_cursor is None
        assert nothing.has_next is False
        assert nothing.has_previous is False


def test_chunks_walk_the_whole_table(db: Database) -> None:
    with db.connect():
        batches = [
            [user.name for user in batch]
            for batch in User.query.order_by(User.id).chunks(2)
        ]

        assert batches == [["a", "b"], ["c", "d"], ["e"]]


def test_chunks_carry_what_the_query_narrows_to(db: Database) -> None:
    with db.connect():
        batches = list(
            User.query.where(User.team == "red").order_by(User.id).chunks(10)
        )

        assert [[user.name for user in batch] for batch in batches] == [["b", "d"]]


def test_from_statement(db: Database) -> None:
    with db.connect():
        users = User.query.from_statement(
            sa.text("SELECT * FROM users WHERE team = 'red' ORDER BY id")
        ).all()

        assert [user.name for user in users] == ["b", "d"]


def test_a_statement_hands_over_its_first_row(db: Database) -> None:
    # `first()` is a read, not a build, so a raw statement answers it too.
    with db.connect():
        query = User.query.from_statement(sa.text("SELECT * FROM users ORDER BY id"))

        first = query.first()

        assert first is not None
        assert first.name == "a"


def test_a_statement_cannot_be_built_on(db: Database) -> None:
    with db.connect():
        query = User.query.from_statement(sa.text("SELECT * FROM users"))
        builds = (
            lambda: query.where(User.id == 1),
            lambda: query.order_by(User.id),
            lambda: query.limit(1),
            lambda: query.count(),
            lambda: query.page(limit=1),
            lambda: query.cursor_page(limit=1),
            # The extension point refuses it too: it used to replace a select
            # nothing reads, and return the statement's own rows.
            lambda: query.with_select(sa.select(User)),
        )

        for build in builds:
            with pytest.raises(RawStatementError):
                build()

        # Copying the query for another database is not building on it.
        assert query.using(db).all()


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str]
    deleted: Mapped[bool] = mapped_column(default=False)

    @classmethod
    def __query_filter__(cls) -> sa.ColumnElement[bool]:
        return cls.deleted.is_(False)


@pytest.fixture
def notes(db: Database) -> Database:
    with db.transaction():
        for index, text in enumerate("abcde"):
            Note(id=index + 1, text=text, deleted=text in "bd").save()
    return db


def test_a_model_filter_applies_to_every_read(notes: Database) -> None:
    with notes.connect():
        assert [note.text for note in Note.query.order_by(Note.id).all()] == [
            "a",
            "c",
            "e",
        ]
        assert Note.query.count() == 3
        assert Note.query.where(Note.text == "b").exists() is False
        assert Note.query.where(Note.text == "b").one_or_none() is None

        page = Note.query.order_by(Note.id).page(limit=2)
        assert [note.text for note in page.items] == ["a", "c"]
        assert page.total == 3

        cursor_page = Note.query.order_by(Note.id).cursor_page(limit=2)
        assert [note.text for note in cursor_page.items] == ["a", "c"]


def test_unfiltered_sees_the_hidden_rows(notes: Database) -> None:
    with notes.connect():
        query = Note.query.unfiltered().order_by(Note.id)

        assert [note.text for note in query.all()] == list("abcde")
        assert query.count() == 5
        assert Note.query.count() == 3  # the branch left the filter alone


def test_a_model_without_a_filter_is_unaffected(db: Database) -> None:
    with db.connect():
        assert User.query.unfiltered().count() == User.query.count()
        assert User.query.with_deleted().count() == User.query.count()
        assert User.query.only_deleted().count() == User.query.count()


# rows a soft delete marks


class Memo(Base, SoftDeletes):
    __tablename__ = "memos"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int]

    @classmethod
    def __query_filter__(cls) -> sa.ColumnElement[bool]:
        return cls.tenant_id == 1


@pytest.fixture
def memos(db: Database) -> Database:
    with db.transaction():
        Memo(id=1, tenant_id=1).save()
        Memo(id=2, tenant_id=1).save()
        Memo(id=3, tenant_id=2).save()
    with db.transaction():
        Memo.query.get_one(1).delete()
    return db


def _rows(db: Database) -> int:
    """How many rows the table holds, marked or not."""
    counted = db.session.scalar(
        sa.select(sa.func.count()).select_from(Base.metadata.tables["memos"])
    )
    return counted or 0


def test_deleting_an_instance_marks_the_row(memos: Database) -> None:
    with memos.connect():
        assert _rows(memos) == 3  # nothing was removed
        assert [memo.id for memo in Memo.query.order_by(Memo.id).all()] == [2]
        assert Memo.query.get(1) is None


def test_the_marked_rows_are_read_on_request(memos: Database) -> None:
    with memos.connect():
        assert [
            memo.id for memo in Memo.query.with_deleted().order_by(Memo.id).all()
        ] == [1, 2]
        assert [
            memo.id for memo in Memo.query.only_deleted().order_by(Memo.id).all()
        ] == [1]


def test_the_two_filters_lift_one_at_a_time(memos: Database) -> None:
    # `unfiltered` is the model's own filter; the mark is its own switch.
    with memos.connect():
        assert [
            memo.id for memo in Memo.query.unfiltered().order_by(Memo.id).all()
        ] == [2, 3]
        assert [
            memo.id
            for memo in Memo.query.unfiltered().with_deleted().order_by(Memo.id).all()
        ] == [1, 2, 3]


def test_restoring_clears_the_mark(memos: Database) -> None:
    with memos.transaction():
        Memo.query.only_deleted().one().restore()

    with memos.connect():
        assert [memo.id for memo in Memo.query.order_by(Memo.id).all()] == [1, 2]


def test_a_bulk_delete_marks_the_rows_too(memos: Database) -> None:
    with memos.transaction():
        assert Memo.query.where(Memo.id == 2).delete() == 1

    with memos.connect():
        assert Memo.query.count() == 0
        assert _rows(memos) == 3
        assert Memo.query.only_deleted().count() == 2


def test_force_removes_the_rows(memos: Database) -> None:
    with memos.transaction():
        assert Memo.query.only_deleted().delete(force=True) == 1

    with memos.transaction():
        Memo.query.get_one(2).delete(force=True)

    with memos.connect():
        assert _rows(memos) == 1  # the other tenant's row


class Chore(Base, SoftDeletes):
    """Soft deletes without a `__query_filter__` of its own."""

    __tablename__ = "chores"

    id: Mapped[int] = mapped_column(primary_key=True)


def test_get_hides_a_marked_row_without_a_query_filter(db: Database) -> None:
    with db.transaction():
        Chore(id=1).save()
    with db.transaction():
        Chore.query.get_one(1).delete()

    with db.transaction():
        assert Chore.query.get(1) is None
        assert Chore.query.with_deleted().get(1) is not None
        assert Chore.query.only_deleted().get(1) is not None

        with pytest.raises(NoResultFound):
            Chore.query.get_one(1)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    members: Mapped[list["Member"]] = relationship(back_populates="team")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    team_id: Mapped[int] = mapped_column(sa.ForeignKey("teams.id"))
    team: Mapped[Team] = relationship(back_populates="members")

    @classmethod
    def __orderable__(cls) -> dict[str, Any]:
        return {"name": cls.name, "team": OrderBy(Team.name, join=cls.team)}


@pytest.fixture
def teams(db: Database) -> Database:
    with db.transaction():
        Team(id=1, name="red").save()
        Member(id=1, name="ada", team_id=1).save()
    return db


def test_join_and_offset(teams: Database) -> None:
    with teams.connect():
        query = (
            Member.query.join(Team)
            .where(Team.name == "red")
            .order_by(Member.id)
            .limit(5)
            .offset(0)
        )

        assert [member.name for member in query.all()] == ["ada"]


@pytest.mark.parametrize(
    "load",
    [
        lambda query: query.joinedload(Member.team),
        lambda query: query.selectinload(Member.team),
        lambda query: query.subqueryload(Member.team),
        lambda query: query.options(joinedload(Member.team)),
    ],
)
def test_loading_a_relationship(
    teams: Database,
    load: Callable[[Any], Any],
) -> None:
    with teams.connect():
        member = load(Member.query).one()

        assert member.team.name == "red"


def test_contains_eager(teams: Database) -> None:
    with teams.connect():
        member = Member.query.join(Team).contains_eager(Member.team).one()

        assert member.team.name == "red"


def test_with_for_update(teams: Database) -> None:
    with teams.transaction():
        # SQLite parses the clause and ignores it; the query still runs.
        assert Member.query.with_for_update(read=True, skip_locked=True).count() == 1


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    at: Mapped[datetime]
    label: Mapped[str | None]


@pytest.fixture
def events(db: Database) -> Database:
    start = datetime(2026, 8, 16, 12, 0)
    with db.transaction():
        for minute, label in enumerate("abcde"):
            Event(at=start + timedelta(minutes=minute), label=label).save()
        Event(at=start + timedelta(minutes=5), label=None).save()
    return db


def test_a_cursor_carries_dates_and_uuids(events: Database) -> None:
    with events.connect():
        seen: list[str] = []
        cursor = None
        while True:
            page = Event.query.order_by(Event.at.desc()).cursor_page(
                limit=2, cursor=cursor
            )
            seen += [event.label for event in page.items]
            cursor = page.next_cursor
            if cursor is None:
                break

        assert seen == [None, *reversed(list("abcde"))]


def test_a_cursor_cannot_start_at_a_null(events: Database) -> None:
    with events.connect():
        # SQLite sorts NULL first, so the page ends on the row without a label.
        with pytest.raises(InvalidCursorError, match="label"):
            Event.query.order_by(Event.label).cursor_page(limit=1)


@pytest.mark.parametrize(
    "payload",
    [
        b"[1, 2]",  # not a cursor of ours at all
        b'{"v": [null, null], "b": false}',  # values nothing compares against
        b'{"v": ["not a date", "x"], "b": false}',  # values of the wrong types
    ],
)
def test_a_forged_cursor_is_refused(events: Database, payload: bytes) -> None:
    forged = base64.urlsafe_b64encode(payload).decode().rstrip("=")

    with events.connect():
        with pytest.raises(InvalidCursorError):
            Event.query.order_by(Event.at).cursor_page(limit=2, cursor=forged)


def test_nested_loading(teams: Database) -> None:
    with teams.connect():
        team = Team.query.joinedload(Team.members, Member.team).one()

        assert team.members[0].team is team


def test_only_columns(db: Database) -> None:
    with db.connect():
        query = User.query.order_by(User.id)

        assert query.only_columns(User.name).all() == list("abcde")
        assert query.only_columns(User.id, User.name).first() == (1, "a")
        assert query.only_columns(User.name).where(User.id == 3).one() == "c"
        assert query.only_columns(User.name).where(User.id == 99).one_or_none() is None
        assert User.query.only_columns(User.team).distinct().order_by(
            User.team
        ).all() == [
            "blue",
            "red",
        ]
        assert User.query.only_columns(User.name).limit(2).offset(1).all() == ["b", "c"]


def test_distinct(db: Database) -> None:
    with db.connect():
        assert User.query.distinct().count() == 5


def test_group_by_and_having(db: Database) -> None:
    with db.connect():
        rows = (
            User.query.group_by(User.team)
            .having(sa.func.count() > 2)
            .only_columns(User.team, sa.func.count())
            .all()
        )

        assert rows == [("blue", 3)]


def test_filter_by_and_select_from(db: Database) -> None:
    with db.connect():
        assert User.query.filter_by(team="red").count() == 2
        assert User.query.select_from(User).filter_by(name="a").one().id == 1


def test_outerjoin(teams: Database) -> None:
    with teams.connect():
        assert Team.query.outerjoin(Member).where(Member.name == "ada").one().id == 1


def test_execution_options(db: Database) -> None:
    with db.connect():
        assert User.query.execution_options(populate_existing=True).count() == 5


def test_create_writes_a_row_and_returns_it(db: Database) -> None:
    with db.transaction():
        user = User.query.create(id=6, name="f", team="green")

        assert (user.id, user.name, user.team) == (6, "f", "green")
        assert User.query.get(6) is user

    with db.connect():
        assert User.query.count() == 6


def test_create_many_writes_one_statement(db: Database) -> None:
    rows = [
        {"id": 7, "name": "g", "team": "red"},
        {"id": 8, "name": "h", "team": "red"},
    ]

    with db.transaction(), assert_queries(1, using=db):
        assert User.query.create_many(rows) == 2

    with db.connect():
        assert [
            user.name for user in User.query.where(User.id > 6).order_by("id").all()
        ] == ["g", "h"]
        assert User.query.create_many([]) == 0


def test_a_write_outside_a_transaction_stays_written(db: Database) -> None:
    # There is nobody to commit it, so the query has to.
    with db.connect():
        User.query.create(id=9, name="i", team="red")
        User.query.create_many([{"id": 10, "name": "j", "team": "red"}])
        User.query.where(User.id == 1).update({"team": "green"})
        User.query.where(User.id == 2).delete()

    with db.connect():
        assert {user.id for user in User.query.all()} == {1, 3, 4, 5, 9, 10}
        assert User.query.get_one(1).team == "green"


def test_bulk_update_and_delete(db: Database) -> None:
    with db.transaction():
        assert User.query.where(User.team == "red").update({"team": "green"}) == 2
        assert User.query.filter_by(team="green").count() == 2
        assert User.query.where(User.name == "a").delete() == 1
        assert User.query.count() == 4


def test_bulk_refuses_what_it_would_drop(db: Database) -> None:
    with db.transaction():
        for build in (
            lambda: User.query.order_by(User.id).delete(),
            lambda: User.query.limit(2).delete(),
            lambda: User.query.offset(2).update({"team": "red"}),
            lambda: User.query.join(User).update({"team": "red"}),
        ):
            with pytest.raises(BulkQueryError):
                build()


def test_page_mapping(db: Database) -> None:
    def names(users: Sequence[User]) -> list[str]:
        return [user.name for user in users]

    with db.connect():
        page = User.query.order_by(User.id).page(limit=2)

        assert page.map(lambda user: user.name).items == ["a", "b"]
        assert page.map_all(names).items == ["a", "b"]
        assert page.with_items(["x", "y"]).total == page.total

        cursor_page = User.query.order_by(User.id).cursor_page(limit=2)

        assert cursor_page.map(lambda user: user.name).items == ["a", "b"]
        assert cursor_page.map_all(names).items == ["a", "b"]
        assert cursor_page.with_items(["x", "y"]).next_cursor == cursor_page.next_cursor


def test_a_transform_returns_one_item_per_row(db: Database) -> None:
    # Totals and cursors belong to the page's rows, so the count is held to.
    with db.connect():
        page = User.query.order_by(User.id).page(limit=2)
        cursor_page = User.query.order_by(User.id).cursor_page(limit=2)

        with pytest.raises(PageItemsMismatchError):
            page.map_all(lambda users: [len(users)])
        with pytest.raises(PageItemsMismatchError):
            page.with_items(["x"])
        with pytest.raises(PageItemsMismatchError):
            cursor_page.with_items(["x"])


def test_a_model_can_name_its_own_cursor_key(db: Database) -> None:
    class Doc(Base):
        __tablename__ = "docs"

        id: Mapped[int] = mapped_column(primary_key=True)
        public_id: Mapped[str] = mapped_column(unique=True)

        @classmethod
        def __cursor_key__(cls) -> tuple[InstrumentedAttribute[str]]:
            return (cls.public_id,)

    with db.connect():
        statement = str(
            Doc.query.order_by(Doc.public_id)._cursor_statement(limit=2, cursor=None)
        )

        assert "ORDER BY docs.public_id ASC" in statement
        assert "docs.id" not in statement.split("ORDER BY")[1]


def test_pagination_needs_an_order(db: Database) -> None:
    # Without one the database is free to return rows in any order, so pages
    # would repeat and skip rows.
    with db.connect():
        with pytest.raises(UnorderedPageError):
            User.query.page(limit=2)

        with pytest.raises(UnorderedPageError):
            User.query.cursor_page(limit=2)


# extending the query


class TeamQuery(Query[Any]):
    def in_team(self, team: str) -> "TeamQuery":
        return self.where(self.model.team == team)


class ScopedBase(ModelMixin, DeclarativeBase):
    query = TeamQuery.as_descriptor()


class Player(ScopedBase):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    team: Mapped[str]
    left_at: Mapped[datetime | None] = mapped_column(default=None)

    @classmethod
    def __query_filter__(cls) -> Any:
        return cls.left_at.is_(None)


@pytest.fixture
def scoped(db: Database) -> Database:
    ScopedBase.set_db(db)
    with db.transaction() as conn:
        ScopedBase.metadata.create_all(conn)
    with db.transaction():
        Player(id=1, team="red").save()
        Player(id=2, team="red", left_at=datetime.now()).save()
        Player(id=3, team="blue").save()
    return db


def test_a_base_hands_its_query_class_to_every_model(scoped: Database) -> None:
    with scoped.transaction():
        assert isinstance(Player.query, TeamQuery)
        assert Player.query.in_team("red").count() == 1


def test_a_custom_method_keeps_its_type_when_chained(scoped: Database) -> None:
    with scoped.transaction():
        query = Player.query.where(Player.id > 0).order_by(Player.id)

        assert isinstance(query, TeamQuery)
        assert [member.id for member in query.in_team("red").all()] == [1]


def test_the_filter_reaches_both_kinds_of_page(scoped: Database) -> None:
    with scoped.transaction():
        page = Player.query.order_by(Player.id).page(limit=10)
        cursor_page = Player.query.order_by(Player.id).cursor_page(limit=10)

        assert [member.id for member in page.items] == [1, 3]
        assert page.total == 2
        assert [member.id for member in cursor_page.items] == [1, 3]


def test_unfiltered_reads_the_hidden_rows(scoped: Database) -> None:
    with scoped.transaction():
        assert Player.query.unfiltered().count() == 3
        assert Player.query.unfiltered().in_team("red").count() == 2


def test_is_ordered(db: Database) -> None:
    assert User.query.is_ordered is False
    assert User.query.order_by(User.id).is_ordered is True


def test_latest_and_earliest(db: Database) -> None:
    with db.transaction():
        latest = User.query.latest(User.id)
        earliest = User.query.earliest(User.id)

        assert latest is not None
        assert earliest is not None
        assert (latest.id, earliest.id) == (5, 1)
        assert User.query.where(User.id > 10).latest(User.id) is None


def test_a_cursor_cannot_page_a_textual_ordering(db: Database) -> None:
    with db.transaction(), pytest.raises(UncomparableOrderingError):
        User.query.order_by(sa.text("name DESC")).cursor_page(limit=2)


def test_a_cursor_cannot_page_an_ordering_the_rows_do_not_carry(
    teams: Database,
) -> None:
    with teams.transaction(), pytest.raises(UncomparableOrderingError):
        Member.query.join(Team).order_by(Team.name).cursor_page(limit=1)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    opened_at: Mapped[datetime] = mapped_column("opened_ts")


def test_a_cursor_reads_a_column_stored_under_another_name(
    db: Database,
) -> None:
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
        now = datetime(2026, 1, 1)
        for index in range(3):
            Ticket(id=index + 1, opened_at=now + timedelta(days=index)).save()

        page = Ticket.query.order_by(Ticket.opened_at).cursor_page(limit=2)
        assert [ticket.id for ticket in page.items] == [1, 2]

        page = Ticket.query.order_by(Ticket.opened_at).cursor_page(
            limit=2, cursor=page.next_cursor
        )
        assert [ticket.id for ticket in page.items] == [3]


class RankedQuery(Query[Any]):
    def top(self) -> "RankedQuery":
        return self.order_by(User.id.desc()).limit(1)


class Ranked(Base):
    __tablename__ = "ranked"

    id: Mapped[int] = mapped_column(primary_key=True)

    query = RankedQuery.as_descriptor()


def test_a_model_can_carry_a_query_of_its_own(db: Database) -> None:
    assert isinstance(Ranked.query, RankedQuery)
    assert isinstance(User.query, Query)
    assert not isinstance(User.query, RankedQuery)


def test_query_get_reads_the_identity_map_and_loads_options(db: Database) -> None:
    with db.transaction():
        user = User.query.get(1)

        assert user is not None
        assert User.query.get(1) is user
        assert User.query.get(99) is None

        with pytest.raises(NoResultFound):
            User.query.get_one(99)


def test_query_get_applies_the_loader_options(teams: Database) -> None:
    with teams.transaction() as conn:
        member = Member.query.joinedload(Member.team).get_one(1)
        conn.execute(sa.text("SELECT 1"))

        assert member.team.name == "red"


def test_query_get_loads_options_onto_a_row_already_in_the_session(
    teams: Database,
) -> None:
    with teams.transaction():
        Member.query.get(1)
        member = Member.query.joinedload(Member.team).get_one(1)

        assert sa.inspect(member).unloaded == set()
        assert member.team.name == "red"


def test_get_refuses_a_query_it_cannot_honour(db: Database) -> None:
    with db.transaction(), pytest.raises(KeyLookupError):
        User.query.where(User.name == "ada").get(1)


class _Statements:
    """Count the statements a block sends to the database."""

    def __init__(self, db: Database) -> None:
        self.engine = db.engine
        self.count = 0

    def __enter__(self) -> Self:
        self.count = 0
        sa.event.listen(self.engine, "before_cursor_execute", self._seen)
        return self

    def __exit__(self, *exc: object) -> None:
        sa.event.remove(self.engine, "before_cursor_execute", self._seen)

    def _seen(self, *_args: object, **_kwargs: object) -> None:
        self.count += 1


def test_a_plain_get_costs_nothing_for_a_row_in_the_session(
    db: Database,
) -> None:
    with db.transaction():
        # Holding the instance is what keeps it in the identity map, which
        # references its rows weakly.
        user = User.query.get(1)

        with _Statements(db) as statements:
            again = User.query.get(1)

        assert again is user
        assert statements.count == 0


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(sa.ForeignKey("teams.id"))
    team: Mapped[Team] = relationship(lazy="raise")
    cancelled: Mapped[bool] = mapped_column(default=False)

    @classmethod
    def __query_filter__(cls) -> sa.ColumnElement[bool]:
        return cls.cancelled.is_(False)


def test_a_filtered_get_loads_options_onto_a_row_in_the_session(
    teams: Database,
) -> None:
    with teams.transaction() as conn:
        Base.metadata.create_all(conn)
    with teams.transaction():
        Booking(id=1, team_id=1).save()
        held = Booking.query.get(1)
        eager = Booking.query.joinedload(Booking.team).get_one(1)

        assert eager is held
        assert eager.team.name == "red"


def test_a_filtered_get_always_asks_the_database(notes: Database) -> None:
    with notes.transaction():
        Note.query.get(1)

        with _Statements(notes) as statements:
            Note.query.get(1)

        # The session knows the row, not whether the filter still admits it.
        assert statements.count == 1


def test_get_hides_what_the_model_hides(notes: Database) -> None:
    with notes.transaction():
        hidden = Note.query.unfiltered().where(Note.deleted.is_(True)).first()

        assert hidden is not None
        assert Note.query.get(hidden.id) is None
        assert Note.query.unfiltered().get(hidden.id) is not None

        with pytest.raises(NoResultFound):
            Note.query.get_one(hidden.id)


def test_get_carries_a_lock_the_query_asks_for(db: Database) -> None:
    with db.transaction():
        assert User.query.with_for_update().get(1) is not None


# sorting by name


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    score: Mapped[int | None] = mapped_column(default=None)
    title: Mapped[str | None] = mapped_column(default=None)

    @classmethod
    def __orderable__(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "score": cls.score,
            "lowered": sa.func.lower(cls.name),
            "late": sa.nulls_last(cls.score),
            "label": sa.nulls_last(cls.title),
            "headline": sa.nulls_first(cls.title),
        }


@pytest.fixture
def reports(db: Database) -> Database:
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    with db.transaction():
        Report(id=1, name="B", score=2, title="Beta").save()
        Report(id=2, name="a", score=None).save()
        Report(id=3, name="c", score=2, title="alpha").save()
    return db


def test_order_by_reads_direction_and_nulls(reports: Database) -> None:
    with reports.transaction():
        assert [r.id for r in Report.query.order_by("name").all()] == [1, 2, 3]
        assert [r.id for r in Report.query.order_by("name.desc").all()] == [3, 2, 1]
        assert [r.id for r in Report.query.order_by("score.asc.nulls_first").all()] == [
            2,
            1,
            3,
        ]
        assert [r.id for r in Report.query.order_by("score.desc.nulls_last").all()] == [
            1,
            3,
            2,
        ]


def test_a_page_breaks_ties_with_the_key(reports: Database) -> None:
    with reports.transaction():
        query = Report.query.order_by("score.desc")

        first = query.page(limit=2)
        second = query.page(limit=2, offset=2)

        assert [r.id for r in first.items] == [3, 1]
        assert [r.id for r in second.items] == [2]


def test_order_by_refuses_a_field_the_model_does_not_offer(
    reports: Database,
) -> None:
    with reports.transaction(), pytest.raises(UnknownOrderFieldError) as caught:
        Report.query.order_by("password")

    assert "name, score" in str(caught.value)


def test_order_by_skips_a_sort_the_request_did_not_name(reports: Database) -> None:
    with reports.transaction():
        assert Report.query.order_by(None).is_ordered is False
        assert [r.id for r in Report.query.order_by("name", None).all()] == [1, 2, 3]


def test_a_model_sorts_by_its_own_columns_by_default(db: Database) -> None:
    with db.transaction():
        ordered = User.query.order_by("name.desc").all()

        assert [user.id for user in ordered] == [5, 4, 3, 2, 1]

        with pytest.raises(UnknownOrderFieldError):
            User.query.order_by("secret")


def test_a_field_can_carry_where_its_nulls_go(reports: Database) -> None:
    with reports.transaction():
        assert [r.id for r in Report.query.order_by("late.desc").all()] == [1, 3, 2]
        assert [r.id for r in Report.query.order_by("late.asc").all()] == [1, 3, 2]
        assert [r.id for r in Report.query.order_by("late.asc.nulls_first").all()] == [
            2,
            1,
            3,
        ]


def test_order_by_takes_a_list_of_fields(reports: Database) -> None:
    with reports.transaction():
        by_list = Report.query.order_by(["score.desc", "name"]).all()
        by_args = Report.query.order_by("score.desc", "name").all()

        assert [r.id for r in by_list] == [r.id for r in by_args] == [1, 3, 2]


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_name: Mapped[str] = mapped_column(default="")
    username: Mapped[str] = mapped_column(default="")
    created_at: Mapped[str] = mapped_column(default="")


def _ordering(*criteria: Any, **options: Any) -> str:
    statement = str(Account.query.order_by(*criteria, **options).select)
    return statement.replace("\n", " ").split("ORDER BY")[1].strip()


@pytest.mark.parametrize(
    "asked",
    ["created_at", "createdAt", "CreatedAt", "CREATED_AT"],
)
def test_order_by_finds_a_field_whichever_case_it_is_asked_in(asked: str) -> None:
    assert _ordering(f"{asked}.desc") == "accounts.created_at DESC"


def test_order_by_takes_the_field_named_exactly(reports: Database) -> None:
    assert _ordering("username") == "accounts.username ASC"
    assert _ordering("user_name") == "accounts.user_name ASC"


def test_order_by_refuses_a_name_that_could_be_two_fields() -> None:
    with pytest.raises(UnknownOrderFieldError, match="userName"):
        Account.query.order_by("userName")


def test_ignore_case_names_the_field_in_any_case(reports: Database) -> None:
    assert _ordering("createdAt", ignore_case=["created_at"]).startswith("lower(")
    assert _ordering("created_at", ignore_case=["createdAt"]).startswith("lower(")


def test_ignore_case_compares_without_regard_to_case(reports: Database) -> None:
    with reports.transaction():
        cased = Report.query.order_by("name").all()
        folded = Report.query.order_by("name", ignore_case=True).all()

        assert [r.name for r in cased] == ["B", "a", "c"]
        assert [r.name for r in folded] == ["a", "B", "c"]


def test_ignore_case_takes_the_fields_it_applies_to(reports: Database) -> None:
    sort = ["label", "name"]  # as a request sends it
    statement = str(Report.query.order_by(sort, ignore_case=["name"]).select)

    assert "lower(reports.name)" in statement
    assert "lower(reports.title)" not in statement


def test_ignore_case_leaves_a_column_that_is_not_text_alone(reports: Database) -> None:
    with reports.transaction():
        ordered = Report.query.order_by("score.desc", ignore_case=True).all()

        assert [r.id for r in ordered] == [1, 3, 2]


def test_ignore_case_keeps_where_a_field_puts_its_nulls(reports: Database) -> None:
    with reports.transaction():
        last = Report.query.order_by("label", ignore_case=True).all()
        first = Report.query.order_by("headline", ignore_case=True).all()

        assert [r.id for r in last] == [3, 1, 2]
        assert [r.id for r in first] == [2, 3, 1]


def test_ignore_case_asks_the_dialect_how(reports: Database) -> None:
    query = Report.query.order_by("name", ignore_case=True)

    assert 'reports.name COLLATE "NOCASE"' in str(query.select.compile(reports.engine))
    assert "lower(reports.name)" in str(query.select)


# without the model layer


class PlainBase(DeclarativeBase):
    pass


class Ticket2(PlainBase):
    __tablename__ = "tickets2"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str]


class TicketRepository:
    """What a query looks like when the models know nothing about sqlakit."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def query(self) -> Query[Ticket2]:
        return Query(Ticket2, self.db)

    def add(self, subject: str) -> Ticket2:
        ticket = Ticket2(subject=subject)
        self.db.session.add(ticket)
        return ticket


def test_a_query_works_without_the_model_layer(db: Database) -> None:
    with db.transaction() as conn:
        PlainBase.metadata.create_all(conn)

    repository = TicketRepository(db)
    with db.transaction():
        for subject in ("a", "b", "c"):
            repository.add(subject)

        page = repository.query.order_by("subject").page(limit=2)
        cursor_page = repository.query.order_by("subject").cursor_page(limit=2)

        assert [ticket.subject for ticket in page.items] == ["a", "b"]
        assert page.total == 3
        assert [ticket.subject for ticket in cursor_page.items] == ["a", "b"]
        assert repository.query.where(Ticket2.subject == "b").count() == 1


def test_a_field_can_bring_the_join_it_needs(teams: Database) -> None:
    with teams.transaction():
        Team(id=2, name="blue").save()
        Member(id=2, name="grace", team_id=2).save()

        query = Member.query.order_by("team")

        assert [member.id for member in query.all()] == [2, 1]
        assert [member.id for member in query.page(limit=2).items] == [2, 1]


def test_a_field_does_not_join_a_table_the_query_already_has(teams: Database) -> None:
    with teams.transaction():
        ordered = Member.query.join(Team).where(Team.name == "red").order_by("team")

        assert str(ordered.select).count("JOIN") == 1
        assert [member.id for member in ordered.all()] == [1]


def test_ordering_by_a_joined_field_twice_joins_once(teams: Database) -> None:
    with teams.transaction():
        ordered = Member.query.order_by("team", "team.desc")

        assert str(ordered.select).count("JOIN") == 1


def test_a_field_can_join_a_subquery_for_an_aggregate(teams: Database) -> None:
    counts = (
        sa.select(Member.team_id, sa.func.count().label("members"))
        .group_by(Member.team_id)
        .subquery()
    )

    class Ranked(Query[Team]):
        def _orderable(self) -> dict[str, Any]:
            return {
                "members": OrderBy(
                    counts.c.members,
                    join=counts,
                    on=counts.c.team_id == Team.id,
                )
            }

    with teams.transaction():
        Team(id=3, name="green").save()
        Member(id=3, name="ada2", team_id=1).save()

        ordered = Ranked(Team, teams).order_by("members.desc").all()

        assert next(team.id for team in ordered) == 1


def test_latest_and_earliest_take_a_field_name(reports: Database) -> None:
    with reports.transaction():
        latest = Report.query.latest("name")
        earliest = Report.query.earliest("name")

        assert latest is not None
        assert earliest is not None
        assert (latest.name, earliest.name) == ("c", "B")


def test_order_by_takes_a_join_of_its_own(teams: Database) -> None:
    with teams.transaction():
        ordered = Member.query.order_by(OrderBy(Team.name.desc(), join=Member.team))

        assert [member.id for member in ordered.all()] == [1]


def test_columns_are_ordered_by_name_too(reports: Database) -> None:
    with reports.transaction():
        names = Report.query.only_columns(Report.name).order_by("name.desc").all()

        assert list(names) == ["c", "a", "B"]


def test_a_page_is_refused_without_an_order_even_when_empty(
    db: Database,
) -> None:
    with db.transaction(), pytest.raises(UnorderedPageError):
        User.query.where(User.id > 100).page(limit=10)


def test_get_takes_a_key_as_a_mapping(notes: Database) -> None:
    with notes.transaction():
        assert Note.query.get({"id": 1}) is not None
        assert Note.query.get({"id": 2}) is None


def test_a_missing_row_names_the_model(db: Database) -> None:
    with db.transaction():
        with pytest.raises(InstanceNotFoundError) as caught:
            User.query.get_one(99)

        assert str(caught.value) == "No User matches this query."
        assert caught.value.model == "User"

        with pytest.raises(NoResultFound):  # SQLAlchemy's own still catches it
            User.query.where(User.id == 99).one()


def test_too_many_rows_name_the_model(db: Database) -> None:
    with db.transaction():
        with pytest.raises(MultipleInstancesFoundError) as caught:
            User.query.one()

        assert str(caught.value) == "More than one User matches this query."

        with pytest.raises(MultipleResultsFound):
            User.query.one_or_none()

        with pytest.raises(MultipleInstancesFoundError):
            User.query.only_columns(User.name).one()


class Listing(Base):
    __tablename__ = "listings"

    __orderable__ = ("title", "price")

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    price: Mapped[int]
    secret: Mapped[str] = mapped_column(default="")


def test_a_model_can_name_the_fields_it_sorts_by(db: Database) -> None:
    with db.transaction() as conn:
        Base.metadata.create_all(conn)
    with db.transaction():
        Listing(id=1, title="b", price=2).save()
        Listing(id=2, title="a", price=1).save()

        assert [row.id for row in Listing.query.order_by("title").all()] == [2, 1]
        assert [row.id for row in Listing.query.order_by("price.desc").all()] == [1, 2]

        with pytest.raises(UnknownOrderFieldError):
            Listing.query.order_by("secret")


def test_a_model_that_says_nothing_sorts_by_every_column(db: Database) -> None:
    with db.transaction():
        assert set(User.query._orderable()) == {"id", "name", "team"}


def test_a_page_breaks_ties_with_its_own_key_not_a_joined_column(
    teams: Database,
) -> None:
    with teams.transaction():
        # `teams.id` is the same name as the model's key and a different column.
        statement = str(
            Member.query.join(Team).order_by(Team.id)._page_statement(limit=2, offset=0)
        )

        assert "ORDER BY teams.id, members.id ASC" in statement


def test_a_declaration_that_names_something_that_is_not_a_column(
    db: Database,
) -> None:
    class Broken(Base):
        __tablename__ = "broken"

        __orderable__ = ("title", "typo")

        id: Mapped[int] = mapped_column(primary_key=True)
        title: Mapped[str]

    with db.transaction(), pytest.raises(InvalidOrderFieldError) as caught:
        Broken.query.order_by("title")

    assert caught.value.field == "typo"
    assert "not a mapped column" in str(caught.value)


def test_two_aliases_of_one_table_are_two_joins(db: Database) -> None:
    home = aliased(Team, name="home")
    away = aliased(Team, name="away")

    class Fixture(Base):
        __tablename__ = "fixtures"

        id: Mapped[int] = mapped_column(primary_key=True)
        home_id: Mapped[int] = mapped_column(sa.ForeignKey("teams.id"))
        away_id: Mapped[int] = mapped_column(sa.ForeignKey("teams.id"))

        @classmethod
        def __orderable__(cls) -> dict[str, Any]:
            return {
                "home": OrderBy(home.name, join=home, on=cls.home_id == home.id),
                "away": OrderBy(away.name, join=away, on=cls.away_id == away.id),
            }

    with db.transaction():
        statement = str(Fixture.query.order_by("home", "away").select)

        assert statement.count("JOIN") == 2
        assert "teams AS home" in statement
        assert "teams AS away" in statement


def test_a_join_target_nobody_can_name_is_joined_rather_than_skipped() -> None:
    select = sa.select(User)

    assert _join_identity(sa.text("whatever")) is None  # inspects to a clause
    assert _join_identity("teams") is None  # inspects to nothing at all
    assert _is_joined(select, "teams") is False


@pytest.mark.parametrize(
    ("sync", "asynchronous"),
    [
        (sqlakit.orm.Query, sqlakit.asyncio.orm.Query),
        (sqlakit.orm.ColumnQuery, sqlakit.asyncio.orm.ColumnQuery),
        (sqlakit.orm.QueryDescriptor, sqlakit.asyncio.orm.QueryDescriptor),
    ],
    ids=lambda cls: cls.__name__,
)
def test_the_async_query_mirrors_this_one(
    sync: type, asynchronous: type, mirrors: Callable[[type, type], None]
) -> None:
    mirrors(sync, asynchronous)
