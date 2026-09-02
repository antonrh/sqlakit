from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Any

from .exceptions import UnknownImportPathError

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["import_models", "import_string"]


def import_string(path: str) -> Any:  # noqa: ANN401
    """Return what a dotted path names, importing what it has to.

    Settings carry a path instead of the thing itself:

    ```python
    db.configure(
        DB_URL,
        routers=["app.db.reports_live_in_the_warehouse"],
    )
    ```

    ``app.db.router`` and ``app.db:router`` both work, and the second is worth
    using when the name could be read as a module.
    """
    module, _, name = path.replace(":", ".").rpartition(".")
    if not module:
        raise UnknownImportPathError(path)
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as error:
        raise UnknownImportPathError(path) from error


def import_models(package: str | ModuleType, *, name: str = "models") -> list[str]:
    """Import every ``models`` module under a package, and return their names.

    A model reaches the metadata when its module is imported, and not before, so
    an application whose models live next to the features that own them has to
    import them all somewhere. What the ones nobody imported cost:

    - `alembic revision --autogenerate` reads a missing model as a table to
      **drop**.
    - `provisioned_tables()` comes up without those tables.
    - A `relationship("Team")` cannot resolve a class nobody has defined.

    Call it once, where the metadata is about to be used:

    ```python
    # migrations/env.py
    import_models("app")
    target_metadata = Model.metadata
    ```

    ``name`` is the module this looks for: ``app/billing/models.py`` and
    everything inside ``app/users/models/``. An import that fails raises, here as
    anywhere.
    """
    if isinstance(package, str):
        package = importlib.import_module(package)
    prefix = f"{package.__name__}."
    suffix = f".{name}"
    inside = f".{name}."

    imported = []
    for _, module, _ in pkgutil.walk_packages(package.__path__, prefix, _reraise):
        if module.endswith(suffix) or inside in module:
            importlib.import_module(module)
            imported.append(module)
    return imported


def _reraise(module: str) -> None:  # noqa: ARG001
    """Let an import error out, rather than walking past the module."""
    raise  # noqa: PLE0704
