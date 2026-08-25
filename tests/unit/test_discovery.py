"""Importing the models an application scattered across its features."""

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from sqlakit import Database, UnknownImportPathError, import_models, import_string

FILES = {
    "app/__init__.py": "",
    "app/billing/__init__.py": "",
    "app/billing/models.py": "from app.base import Base\n"
    "import sqlalchemy as sa\n"
    "class Invoice(Base):\n"
    "    __tablename__ = 'invoices'\n"
    "    id = sa.Column(sa.Integer, primary_key=True)\n",
    "app/users/__init__.py": "",
    "app/users/models/__init__.py": "",
    "app/users/models/user.py": "from app.base import Base\n"
    "import sqlalchemy as sa\n"
    "class User(Base):\n"
    "    __tablename__ = 'users'\n"
    "    id = sa.Column(sa.Integer, primary_key=True)\n",
    "app/users/services.py": "raise AssertionError('nobody asked for this one')\n",
    "app/base.py": "from sqlalchemy.orm import DeclarativeBase\n"
    "class Base(DeclarativeBase):\n"
    "    pass\n",
}


@pytest.fixture
def application(tmp_path: Path) -> Iterator[Path]:
    for name, source in FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    sys.path.insert(0, str(tmp_path))
    yield tmp_path
    sys.path.remove(str(tmp_path))
    for name in [n for n in sys.modules if n == "app" or n.startswith("app.")]:
        del sys.modules[name]


def test_the_models_reach_the_metadata(application: Path) -> None:
    base = importlib.import_module("app.base")

    assert base.Base.metadata.tables == {}

    imported = import_models("app")

    assert sorted(imported) == [
        "app.billing.models",
        "app.users.models",
        "app.users.models.user",
    ]
    assert sorted(base.Base.metadata.tables) == ["invoices", "users"]


def test_a_module_that_is_not_a_model_is_left_alone(application: Path) -> None:
    # `app.users.services` raises on import; walking past it is the point.
    import_models("app")

    assert "app.users.services" not in sys.modules


def test_a_package_may_be_handed_over_as_a_module(application: Path) -> None:
    package = importlib.import_module("app")

    assert import_models(package, name="models")


def test_a_package_that_fails_to_import_is_not_walked_past(
    application: Path,
) -> None:
    broken = application / "app" / "wrecked"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("import nothing_here\n")

    with pytest.raises(ModuleNotFoundError):
        import_models("app")


def test_an_import_that_fails_is_not_swallowed(application: Path) -> None:
    (application / "app" / "broken" / "").mkdir(parents=True, exist_ok=True)
    (application / "app" / "broken" / "__init__.py").write_text("")
    (application / "app" / "broken" / "models.py").write_text("import nothing_here\n")

    with pytest.raises(ModuleNotFoundError):
        import_models("app")


def test_what_it_saves_a_schema_from(application: Path) -> None:
    base = importlib.import_module("app.base")

    db = Database("sqlite://", engine_args={"poolclass": sa.StaticPool})

    with (
        db.provisioned_tables(base.Base.metadata),
        db.connect() as connection,
    ):
        # Nothing was imported, so the metadata is empty and so is the schema.
        assert sa.inspect(connection).get_table_names() == []

    import_models("app")

    with (
        db.provisioned_tables(base.Base.metadata),
        db.connect() as connection,
    ):
        assert sorted(sa.inspect(connection).get_table_names()) == ["invoices", "users"]


def test_an_import_path_with_no_module_is_refused() -> None:
    with pytest.raises(UnknownImportPathError, match=r"^`router`"):
        import_string("router")
