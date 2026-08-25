"""`examples/flask_app.py`, run as a service."""

import os
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from dirty_equals import IsInt, IsStr
from flask.testing import FlaskClient

# The example reads its URL when it is imported, so it is named before that.
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/flask.db"

from examples import flask_app


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    with flask_app.db.transaction() as conn:
        flask_app.Model.metadata.create_all(conn)


@pytest.fixture
def client() -> Iterator[FlaskClient]:
    with flask_app.app.test_client() as client:
        yield client

    with flask_app.db.transaction() as conn:
        conn.execute(sa.delete(flask_app.User))


def _create(client: FlaskClient, name: str, team: str = "red") -> dict[str, Any]:
    response = client.post("/users", json={"name": name, "team": team})
    assert response.status_code == 201
    return response.get_json()


def test_writing_and_reading_one_user(client: FlaskClient) -> None:
    created = _create(client, "ada")

    assert created == {"id": IsInt(), "name": "ada", "team": "red"}
    assert client.get(f"/users/{created['id']}").get_json() == created


def test_a_page_with_a_total(client: FlaskClient) -> None:
    for name in ("ada", "grace", "alan"):
        _create(client, name)

    page = client.get("/users", query_string={"limit": 2})

    assert page.status_code == 200
    assert page.get_json() == {
        "items": [
            {"id": IsInt(), "name": "alan", "team": "red"},
            {"id": IsInt(), "name": "grace", "team": "red"},
        ],
        "total": 3,
        "has_next": True,
    }


def test_a_page_the_caller_scrolls(client: FlaskClient) -> None:
    for name in ("ada", "grace", "alan"):
        _create(client, name)

    first = client.get("/feed", query_string={"limit": 2}).get_json()
    second = client.get(
        "/feed", query_string={"limit": 2, "cursor": first["next_cursor"]}
    ).get_json()
    back = client.get(
        "/feed", query_string={"limit": 2, "cursor": second["previous_cursor"]}
    ).get_json()

    assert first == {
        "items": [
            {"id": IsInt(), "name": "alan", "team": "red"},
            {"id": IsInt(), "name": "grace", "team": "red"},
        ],
        "next_cursor": IsStr(),
        "previous_cursor": None,
    }
    assert second == {
        "items": [{"id": IsInt(), "name": "ada", "team": "red"}],
        "next_cursor": None,
        "previous_cursor": IsStr(),
    }
    assert back == first


def test_a_missing_user_is_a_404(client: FlaskClient) -> None:
    response = client.get("/users/404")

    assert response.status_code == 404
    assert response.get_json() == {"detail": "User not found"}


def test_a_sort_the_model_does_not_offer_is_a_400(client: FlaskClient) -> None:
    response = client.get("/users", query_string={"sort": "secret"})

    assert response.status_code == 400
    assert response.get_json() == {
        "detail": "`secret` is not something this model orders by. "
        "It offers: created_at, id, name, team."
    }


def test_a_write_that_touches_many_rows(client: FlaskClient) -> None:
    for name in ("ada", "grace"):
        _create(client, name, team="red")
    _create(client, "alan", team="blue")

    moved = client.post("/teams/red/rename", query_string={"to": "green"})

    assert moved.get_json() == {"moved": 2}
    assert client.get("/users").get_json() == {
        "items": [
            {"id": IsInt(), "name": "alan", "team": "blue"},
            {"id": IsInt(), "name": "grace", "team": "green"},
            {"id": IsInt(), "name": "ada", "team": "green"},
        ],
        "total": 3,
        "has_next": False,
    }


def test_the_health_endpoint_needs_no_block(client: FlaskClient) -> None:
    assert client.get("/health").get_json() == {"database": True}
