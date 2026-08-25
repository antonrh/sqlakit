from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["Router", "as_router"]


@runtime_checkable
class Router(Protocol):
    """A policy that says which database a model lives on.

    Inherit it for the state a policy needs, or hand `route()` a plain function.
    Both are asked the same question:

    ```python
    db.route(Sharding(SHARDS))
    db.route(lambda model: SHARDS.get(model))
    ```

    The contract, either way:

    - It takes the model class, and nothing else. Not an instance, not the
      statement, not whether it reads or writes: placement is a property of the
      model.
    - It returns the alias the model lives on, or None to leave the question to
      the next router, and then to the model's own ``__db__``.
    - The alias has to be one the registry was configured with.
    - It is asked every time a model resolves its database, so keep it cheap and
      keep the answer the same for the same model.
    """

    def db_for(self, model: type[Any], /) -> str | None:
        """Return the alias this model lives on, or None to say nothing."""
        ...


def as_router(router: Router | Callable[[type[Any]], str | None]) -> Any:  # noqa: ANN401
    """Return what to ask, whichever of the two shapes came in."""
    question = getattr(router, "db_for", None)
    return router if question is None else question
