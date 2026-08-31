from collections.abc import Iterable

from sqlalchemy.orm import exc as sa_exc

DEFAULT_ALIAS = "default"

__all__ = [
    "AliasInUseError",
    "AsyncFilterError",
    "BulkQueryError",
    "ConflictingDatabaseUrlError",
    "DatabaseAlreadyConfiguredError",
    "DatabaseNotConfiguredError",
    "DefaultAliasError",
    "DetachedInstanceError",
    "InstanceNotFoundError",
    "InvalidCursorError",
    "InvalidDatabaseConfigError",
    "InvalidNullsError",
    "InvalidOrderFieldError",
    "MissingConnectionError",
    "MissingDatabaseUrlError",
    "MissingDefaultDatabaseError",
    "MissingDependencyError",
    "MissingSessionError",
    "MultipleInstancesFoundError",
    "NullCursorValueError",
    "RawStatementError",
    "RetryNotSupportedError",
    "SQLAKitError",
    "SQLNotConfiguredError",
    "StrayParameterError",
    "TemplateNotFoundError",
    "TransactionRolledBackError",
    "UnknownDatabaseError",
    "UnknownFieldError",
    "UnknownImportPathError",
    "UnorderedPageError",
]


class SQLAKitError(Exception):
    """Base class for all sqlakit errors."""


class MissingConnectionError(SQLAKitError, RuntimeError):
    """Raised when no connection is bound to the current context."""

    def __init__(
        self,
        message: str = (
            "No connection is bound to the current context. "
            "Enter `Database.connect()` or `Database.transaction()` first."
        ),
    ) -> None:
        super().__init__(message)


class MissingSessionError(SQLAKitError, RuntimeError):
    """Raised when no session is bound to the current context."""

    def __init__(
        self,
        message: str = (
            "No session is bound to the current context. "
            "Enter `Database.session_factory()` or `Database.connect()` first."
        ),
    ) -> None:
        super().__init__(message)


class RetryNotSupportedError(SQLAKitError, TypeError):
    """Raised when a transaction with ``retry_on`` is entered as a block."""

    def __init__(
        self,
        message: str = (
            "A transaction with `retry_on` cannot be used as a context manager: "
            "retrying re-runs the block, which only a decorator can do. "
            "Use `@db.transaction(retry_on=...)` instead."
        ),
    ) -> None:
        super().__init__(message)


class DatabaseNotConfiguredError(SQLAKitError, RuntimeError):
    """Raised when the importable database is used before it has a URL."""

    def __init__(
        self,
        message: str = (
            "The database is not configured. "
            "Call `db.configure(url, ...)` once, at startup, before using it."
        ),
    ) -> None:
        super().__init__(message)


class DatabaseAlreadyConfiguredError(SQLAKitError, RuntimeError):
    """Raised when reconfiguring a database whose engine is already in use."""

    def __init__(
        self,
        message: str = (
            "The database is already connected. Dispose of the engine before "
            "configuring it again."
        ),
    ) -> None:
        super().__init__(message)


class AliasInUseError(SQLAKitError, ValueError):
    """Raised when registering a database under an alias another one holds."""

    def __init__(self, alias: str) -> None:
        self.alias = alias
        super().__init__(
            f"`{alias}` is already registered. Dispose of that database before "
            "registering another under the same name."
        )


class DefaultAliasError(SQLAKitError, ValueError):
    """Raised when registering a database as the default one."""

    def __init__(self) -> None:
        super().__init__(
            f"`{DEFAULT_ALIAS}` is the registry itself. Configure it with "
            "`configure()`, and register the others under their own names."
        )


class UnknownDatabaseError(SQLAKitError, KeyError):
    """Raised when asking for a database alias that was never configured."""

    def __init__(self, alias: str, known: tuple[str, ...] = ()) -> None:
        super().__init__(
            f"No database is configured as {alias!r}. "
            f"Configured: {', '.join(map(repr, known)) or 'none'}."
        )


class MissingDefaultDatabaseError(SQLAKitError, ValueError):
    """Raised when a configuration keyed by alias carries no default."""

    def __init__(self, aliases: tuple[str, ...] = ()) -> None:
        super().__init__(
            f"A configuration keyed by alias has to carry a {DEFAULT_ALIAS!r}: "
            f"that is the database reached without naming an alias. "
            f"Got: {', '.join(map(repr, aliases)) or 'nothing'}."
        )


class InvalidDatabaseConfigError(SQLAKitError, ValueError):
    """Raised when a configuration cannot be turned into a database."""


class MissingDatabaseUrlError(InvalidDatabaseConfigError):
    """Raised when a configuration says nowhere to connect."""

    def __init__(
        self,
        message: str = (
            "A database has to be configured with a `url`, or with at least a "
            "`drivername` to build one from."
        ),
    ) -> None:
        super().__init__(message)


class ConflictingDatabaseUrlError(InvalidDatabaseConfigError):
    """Raised when a configuration says where to connect twice over."""

    def __init__(self, parts: tuple[str, ...] = ()) -> None:
        super().__init__(
            "A database is configured with a `url` or with its parts "
            f"({', '.join(parts)}), not with both."
        )


class TransactionRolledBackError(SQLAKitError, RuntimeError):
    """Raised when a block finds its transaction already rolled back."""

    def __init__(
        self,
        message: str = (
            "The transaction was rolled back from inside the block, leaving "
            "nothing to commit. A session that only takes part in a "
            "transaction rolls back the whole of it; for a block that has to "
            "fail on its own, use `transaction(savepoint=True)`."
        ),
    ) -> None:
        super().__init__(message)


class DetachedInstanceError(SQLAKitError, sa_exc.DetachedInstanceError):
    """Raised when saving an instance whose session is gone.

    A subclass of SQLAlchemy's error of the same name, so code that catches
    either one catches this.
    """

    def __init__(self, model: str) -> None:
        super().__init__(
            f"This {model} belongs to a session that has closed, so saving it "
            f"would silently copy it into another one. Load it again in this "
            f"block, or merge it yourself with `session.merge(...)`."
        )


class InstanceNotFoundError(SQLAKitError, sa_exc.NoResultFound):
    """Raised when a query that must match one instance matches none.

    A subclass of SQLAlchemy's `NoResultFound`, so code that catches either one
    catches this. It names the model, which is what an API answering 404 wants:

    ```python
    except InstanceNotFoundError as error:
        raise HTTPException(404, f"{error.model} not found")
    ```
    """

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"No {model} matches this query.")


class MultipleInstancesFoundError(SQLAKitError, sa_exc.MultipleResultsFound):
    """Raised when a query that must match one instance matches several.

    A subclass of SQLAlchemy's `MultipleResultsFound`, so code that catches
    either one catches this. Reaching it means the query is not as narrow as
    the call assumed, or the column it narrows on is not unique.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"More than one {model} matches this query.")


class InvalidCursorError(SQLAKitError, ValueError):
    """Raised when a pagination cursor was not made here, or not for this order."""

    def __init__(
        self,
        message: str = (
            "This cursor does not belong to this query. Pass back the one the "
            "previous page returned, unchanged, and order the query the same way."
        ),
    ) -> None:
        super().__init__(message)


class MissingDependencyError(SQLAKitError, ImportError):
    """Raised when something optional is used and its package is not installed."""

    def __init__(
        self, package: str, needed_by: str, install: str | None = None
    ) -> None:
        self.package = package
        super().__init__(
            f"`{package}` is not installed, and {needed_by} cannot work "
            f"without it. Install `{install or package}`."
        )


class SQLNotConfiguredError(SQLAKitError, RuntimeError):
    """Raised when a database is asked for a template file and has no path."""

    def __init__(
        self,
        message: str = (
            "This database does not know where its SQL templates are. Pass "
            "`templates=` to `Database(...)` or to `db.configure(...)`, or "
            "write the SQL out with `db.sql.from_string(...)`."
        ),
    ) -> None:
        super().__init__(message)


class TemplateNotFoundError(SQLAKitError, FileNotFoundError):
    """Raised when no configured path holds the template asked for."""

    def __init__(self, template: str, paths: Iterable[object] = ()) -> None:
        self.template = template
        looked = ", ".join(str(path) for path in paths)
        super().__init__(
            f"No SQL template named `{template}`. Looked in: {looked or 'nowhere'}."
        )


class AsyncFilterError(SQLAKitError, TypeError):
    """Raised when a template is given a filter or global that has to be awaited."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"`{name}` is a coroutine function, and templates render "
            f"synchronously: rendering builds SQL and awaits nothing, in the "
            f"async API as well. Await the value first, and pass what it "
            f"returns in the context."
        )


class StrayParameterError(SQLAKitError, ValueError):
    """Raised when rendered SQL holds a parameter the template never bound."""

    def __init__(self, names: Iterable[str] = (), template: str | None = None) -> None:
        self.names = tuple(names)
        where = f"`{template}`" if template else "This SQL"
        listed = ", ".join(f"`:{name}`" for name in self.names)
        written = ", ".join(f"`{{{{ {name} }}}}`" for name in self.names)
        super().__init__(
            f"{where} reads as though {listed} were a parameter, and nothing "
            f"binds it. Values come from the template: write {written} and "
            f"pass them in the context. A colon that belongs to the SQL, "
            f"inside a JSON document or a string that starts with one, is "
            f"written `\\:`."
        )


class RawStatementError(SQLAKitError, TypeError):
    """Raised when building on a query that already carries a statement."""

    def __init__(self, method: str = "this", advice: str | None = None) -> None:
        super().__init__(
            f"A query built from a statement cannot be narrowed further: "
            f"{advice or f'put `{method}` in the statement itself'}."
        )


class NullCursorValueError(InvalidCursorError):
    """Raised when a page would have to start at a row that is NULL in the order."""

    def __init__(self, column: str = "the ordering") -> None:
        super().__init__(
            f"Cannot page past a row whose `{column}` is NULL: comparing against "
            f"NULL matches nothing. Order by a column that is never NULL, or "
            f"add one."
        )


class BulkQueryError(SQLAKitError, TypeError):
    """Raised when a bulk update or delete is asked to honour what it cannot."""

    def __init__(self, method: str = "update", dropped: tuple[str, ...] = ()) -> None:
        super().__init__(
            f"A bulk `{method}` writes one statement, which has no room for "
            f"{', '.join(dropped) or 'this'}. Narrow it with `where` alone, or "
            f"read the rows and write them one by one."
        )


class PageItemsMismatchError(SQLAKitError, TypeError):
    """Raised when a page transform returns the wrong number of items."""

    def __init__(self, expected: int = 0, got: int = 0) -> None:
        super().__init__(
            f"The page holds {expected} items and the transform returned {got}. "
            f"Totals and cursors belong to the page's rows, so a transform has "
            f"to return one item per row."
        )


class UnknownOrderFieldError(SQLAKitError, ValueError):
    """Raised when an ordering names a field the model does not offer."""

    def __init__(self, field: str, orderable: Iterable[str] = ()) -> None:
        offered = ", ".join(sorted(orderable))
        super().__init__(
            f"`{field}` is not something this model orders by. "
            f"It offers: {offered or 'nothing'}."
        )


class UnknownImportPathError(SQLAKitError, ImportError):
    """Raised when a dotted path names nothing that can be imported."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(
            f"`{path}` does not name anything importable. A path is "
            f"`package.module.name`, or `package.module:name` when the name "
            f"could be read as a module of its own."
        )


class UnknownFieldError(SQLAKitError, AttributeError):
    """Raised when a model is handed a field it does not have."""

    def __init__(self, model: str, field: str) -> None:
        self.model = model
        self.field = field
        super().__init__(f"`{model}` has no field `{field}`.")


class InvalidOrderFieldError(SQLAKitError, TypeError):
    """Raised when ``__orderable__`` names something the model does not have.

    A mistake in the declaration rather than in the request, which is why it is
    not the error a bad sort string raises: answering a client with 400 for it
    would blame the wrong side.
    """

    def __init__(self, model: str, field: str) -> None:
        self.model = model
        self.field = field
        super().__init__(
            f"`{model}.__orderable__` names `{field}`, which is not a mapped "
            f"column of it. Name a column, or return a mapping from a "
            f"classmethod for fields that are not columns."
        )


class InvalidNullsError(SQLAKitError, ValueError):
    """Raised when ``order_by`` is told to put the nulls somewhere else.

    ``nulls`` says where the rows with no value go, and SQL has two answers to
    that.
    """

    def __init__(self, nulls: object) -> None:
        self.nulls = nulls
        super().__init__(f"`nulls` is `first` or `last`, not `{nulls!r}`.")


class KeyLookupError(SQLAKitError, TypeError):
    """Raised when a lookup by primary key is asked to honour what it cannot."""

    def __init__(self, carried: tuple[str, ...] = ()) -> None:
        super().__init__(
            f"`get` looks a row up by its primary key, which has no room for "
            f"{', '.join(carried) or 'this'}. Read the rows the query narrows "
            f"to with `one`, `first` or `all` instead."
        )


class UncomparableOrderingError(SQLAKitError, TypeError):
    """Raised when a cursor page is ordered by something it cannot compare."""

    def __init__(self, clause: object) -> None:
        super().__init__(
            f"A cursor cannot page an ordering by `{clause}`. It compares rows "
            f"by the values it reads back from them, so the order has to name "
            f"columns of the model rather than text or an expression."
        )


class UnorderedPageError(SQLAKitError, TypeError):
    """Raised when a page is read from a query that names no order."""

    def __init__(
        self,
        message: str = (
            "Pagination needs an order. Without one the database returns rows "
            "in whatever order it finds them, so pages repeat and skip rows. "
            "Add `.order_by(...)`."
        ),
    ) -> None:
        super().__init__(message)
