# Examples

Complete programs, not snippets. Each one is a file in the repository under
[`examples/`](https://github.com/antonrh/sqlakit/tree/main/examples), and each
one is run by the test suite, so the code on this page is tested code.

| | |
| --- | --- |
| [A FastAPI service](#a-fastapi-service) | endpoints, transactions, limit-offset and cursor pages, error handling for an API |
| [A Flask service](#a-flask-service) | the same endpoints, synchronous, with blocks on the views |
| [SQLModel, the data mapper way](#sqlmodel-the-data-mapper-way) | plain models, repositories that hold the database |
| [SQLModel, the Active Record way](#sqlmodel-the-active-record-way) | the same models saving and reading themselves |

## A `FastAPI` service {#a-fastapi-service}

One database, a transaction on each endpoint that needs one, and the errors a
query raises handled in one place. `@db.transaction` goes under `@app.get`, so
the request runs in a transaction that commits when the handler returns and
rolls back when it raises. The decorator keeps the handler's signature, so
`Depends` and `FastAPI`'s parameter parsing work as usual.

```python title="examples/fastapi_app.py"
--8<-- "examples/fastapi_app.py"
```

## A `Flask` service {#a-flask-service}

The same endpoints without `asyncio`. `db.transaction` and `db.autocommit` sit
under the route decorator, and the errors a query raises are handled by
`errorhandler`.

```python title="examples/flask_app.py"
--8<-- "examples/flask_app.py"
```

## `SQLModel`, the data mapper way {#sqlmodel-the-data-mapper-way}

`SQLModel` classes are `SQLAlchemy` models, so a query works on them like any
other. Nothing here inherits from SQLAKit: the repository holds the database
and builds the queries.

```python title="examples/sqlmodel_datamapper.py"
--8<-- "examples/sqlmodel_datamapper.py"
```

## `SQLModel`, the Active Record way {#sqlmodel-the-active-record-way}

The same application with the [model layer](models.md): `save()`, `delete()`
and `Model.query` on the classes themselves. `ModelMixin` is inherited second,
after `SQLModel`. A custom query is annotated as a `ClassVar`: every class
attribute of a `pydantic` model needs an annotation, and `ClassVar` marks this
attribute as not a column.

```python title="examples/sqlmodel_activerecord.py"
--8<-- "examples/sqlmodel_activerecord.py"
```

Next: [queries](queries.md) for the builder these use, or
[testing](testing.md) for the fixtures that run them.
