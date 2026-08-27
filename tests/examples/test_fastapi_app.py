"""`examples/fastapi_app.py`, run as a service."""

import os
import tempfile
from collections.abc import Iterator

import anyio
import pytest
import sqlalchemy as sa
from dirty_equals import IsInt, IsStr
from fastapi.testclient import TestClient

# The example reads its URL when it is imported, so it is named before that.
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/app.db"

from examples import fastapi_app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(fastapi_app.app) as client:
        yield client

    async def empty_the_table() -> None:
        async with fastapi_app.db.transaction() as conn:
            await conn.execute(sa.delete(fastapi_app.User))
        # The lifespan disposed of the engine this reopened; dispose again, or
        # its `aiosqlite` thread outlives the test.
        await fastapi_app.db.dispose()

    anyio.run(empty_the_table)


def _create(client: TestClient, name: str, team: str = "red") -> dict:
    response = client.post("/users", json={"name": name, "team": team})
    assert response.status_code == 201
    return response.json()


def test_writing_and_reading_one_user(client: TestClient) -> None:
    created = _create(client, "ada")

    assert client.get(f"/users/{created['id']}").json() == created


def test_a_page_the_caller_numbers(client: TestClient) -> None:
    for name in ("ada", "grace", "alan"):
        _create(client, name)

    page = client.get("/users", params={"limit": 2})

    assert page.status_code == 200
    assert page.json() == {
        "items": [
            {"id": IsInt(), "name": "alan", "team": "red"},
            {"id": IsInt(), "name": "grace", "team": "red"},
        ],
        "total": 3,
        "has_next": True,
    }


def test_a_page_the_caller_scrolls(client: TestClient) -> None:
    for name in ("ada", "grace", "alan"):
        _create(client, name)

    first = client.get("/feed", params={"limit": 2}).json()
    second = client.get("/feed", params={"limit": 2, "cursor": first["next_cursor"]})
    back = client.get(
        "/feed", params={"limit": 2, "cursor": second.json()["previous_cursor"]}
    )

    assert first == {
        "items": [
            {"id": IsInt(), "name": "alan", "team": "red"},
            {"id": IsInt(), "name": "grace", "team": "red"},
        ],
        "next_cursor": IsStr(),
        "previous_cursor": None,
    }
    assert second.json() == {
        "items": [{"id": IsInt(), "name": "ada", "team": "red"}],
        "next_cursor": None,
        "previous_cursor": IsStr(),
    }
    assert back.json() == first


def test_a_missing_user_is_a_404(client: TestClient) -> None:
    response = client.get("/users/404")

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}


def test_a_sort_the_model_does_not_offer_is_a_400(client: TestClient) -> None:
    response = client.get("/users", params={"sort": "secret"})

    assert response.status_code == 400
    assert response.json() == {
        "detail": "`secret` is not something this model orders by. "
        "It offers: created_at, id, name, team."
    }


def test_writing_to_many_rows_in_one_statement(client: TestClient) -> None:
    _create(client, "ada", team="red")
    _create(client, "grace", team="red")
    _create(client, "alan", team="blue")

    assert client.post("/teams/red/rename", params={"to": "green"}).json() == {
        "moved": 2
    }
    assert client.get("/users").json() == {
        "items": [
            {"id": IsInt(), "name": "alan", "team": "blue"},
            {"id": IsInt(), "name": "grace", "team": "green"},
            {"id": IsInt(), "name": "ada", "team": "green"},
        ],
        "total": 3,
        "has_next": False,
    }


def test_health_needs_no_transaction(client: TestClient) -> None:
    assert client.get("/health").json() == {"database": True}
