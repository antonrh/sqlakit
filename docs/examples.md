# Examples

Whole programs, not snippets. Each one is a file in the repository under
[`examples/`](https://github.com/antonrh/sqlakit/tree/main/examples), and each
one is run by the test suite, so the code here is the code that passes.

| | |
| --- | --- |
| [A FastAPI service](#a-fastapi-service) | endpoints, transactions, limit-offset and cursor pages, the errors an API answers |
| [A Flask service](#a-flask-service) | the same endpoints, synchronous, with blocks on the views |
| [SQLModel, the data mapper way](#sqlmodel-the-data-mapper-way) | plain models, repositories that hold the database |
| [SQLModel, the Active Record way](#sqlmodel-the-active-record-way) | the same models saving and reading themselves |

## A `FastAPI` service {#a-fastapi-service}

One database, a transaction named on the endpoint that needs one, and the errors
a query raises answered in one place. `@db.transaction` goes under `@app.get`,
so the request runs in a transaction that commits when the handler returns and
rolls back when it raises. The decorator keeps the handler's signature, so
`Depends` and `FastAPI`'s parameter parsing work as they always do.

```python title="examples/fastapi_app.py"
--8<-- "examples/fastapi_app.py"
```

## A `Flask` service {#a-flask-service}

The same endpoints without `asyncio`. `db.transaction` and `db.autocommit` sit
under the route decorator, and the errors a query raises are answered by
`errorhandler`.

```python title="examples/flask_app.py"
--8<-- "examples/flask_app.py"
```

## `SQLModel`, the data mapper way {#sqlmodel-the-data-mapper-way}

`SQLModel` classes are `SQLAlchemy` models, so a query works on them like any
other. Nothing here inherits from SQLAKit: the repository holds the database and
hands out queries.

```python title="examples/sqlmodel_datamapper.py"
--8<-- "examples/sqlmodel_datamapper.py"
```

## `SQLModel`, the Active Record way {#sqlmodel-the-active-record-way}

The same application with the [model layer](models.md): `save()`, `delete()` and
`Model.query` on the classes themselves. `ModelMixin` is inherited second, after
`SQLModel`. A query of your own is annotated as a `ClassVar`. Every class
attribute of a `pydantic` model wants an annotation, and `ClassVar` is how you
say this attribute is not a column.

```python title="examples/sqlmodel_activerecord.py"
--8<-- "examples/sqlmodel_activerecord.py"
```

Next: [queries](queries.md) for the builder these use, or
[testing](testing.md) for the fixtures that run them.
