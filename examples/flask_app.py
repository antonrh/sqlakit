"""A small Flask service on SQLAKit.

Run it with `flask --app examples.flask_app run`, or read it as the shape of a
synchronous application: one db, a transaction named on each view, and the
errors a query raises answered in one place.
"""

import os
from datetime import UTC, datetime
from typing import Any

from flask import Flask, jsonify, request
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.wrappers import Response

from sqlakit import Database, InstanceNotFoundError, UnknownOrderFieldError
from sqlakit.orm import Model

db = Database(os.environ.get("DATABASE_URL", "sqlite:///app.db"))


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- The models ---


class Base(Model):
    __abstract__ = True


Base.set_db(db)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str]
    team: Mapped[str] = mapped_column(default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "team": self.team}


# --- The application ---


app = Flask(__name__)


@app.errorhandler(InstanceNotFoundError)
def not_found(error: InstanceNotFoundError) -> tuple[Response, int]:
    return jsonify({"detail": f"{error.model} not found"}), 404


@app.errorhandler(UnknownOrderFieldError)
def bad_sort(error: UnknownOrderFieldError) -> tuple[Response, int]:
    return jsonify({"detail": str(error)}), 400


# --- The views ---


@app.post("/users")
@db.transaction
def create_user() -> tuple[Response, int]:
    payload = request.get_json()
    user = User(name=payload["name"], team=payload.get("team", "")).save()
    return jsonify(user.as_json()), 201


@app.get("/users/<int:user_id>")
@db.autocommit
def get_user(user_id: int) -> Response:
    return jsonify(User.query.get_one(user_id).as_json())


@app.get("/users")
@db.autocommit
def list_users() -> Response:
    page = User.query.order_by(request.args.get("sort", "created_at.desc")).page(
        limit=request.args.get("limit", 20, type=int),
        offset=request.args.get("offset", 0, type=int),
    )
    return jsonify(
        {
            "items": [user.as_json() for user in page.items],
            "total": page.total,
            "has_next": page.has_next,
        }
    )


@app.get("/feed")
@db.autocommit
def feed() -> Response:
    page = User.query.order_by("created_at.desc").cursor_page(
        limit=request.args.get("limit", 20, type=int),
        cursor=request.args.get("cursor"),
    )
    return jsonify(
        {
            "items": [user.as_json() for user in page.items],
            "next_cursor": page.next_cursor,
            "previous_cursor": page.previous_cursor,
        }
    )


@app.post("/teams/<team>/rename")
@db.transaction
def rename_team(team: str) -> Response:
    moved = User.query.where(User.team == team).update(
        {"team": request.args["to"]},
    )
    return jsonify({"moved": moved})


@app.get("/health")
def health() -> Response:
    return jsonify({"database": db.ping()})


if __name__ == "__main__":
    with db.transaction() as conn:
        Model.metadata.create_all(conn)
    app.run()
