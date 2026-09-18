# Compared to SQLAlchemy

Connection, session and transaction management, in the situations where the
two differ. `add_user()` is one function of the application, called from a
job, on its own, from a handler and from a test. Imports are left out.

<table markdown="1">
<thead>
<tr>
<th></th>
<th markdown="block">`SQLAKit`</th>
<th markdown="block">`SQLAlchemy`</th>
</tr>
</thead>
<tbody>

<tr>
<td markdown="block">Setup</td>
<td markdown="block">

```python
db = Database(url)
```

</td>
<td markdown="block">

```python
engine = create_engine(url)
SessionFactory = sessionmaker(engine)
```

</td>
</tr>

<tr>
<td markdown="block">A function that writes</td>
<td markdown="block">

```python
@db.transaction
def add_user(name: str) -> User:
    user = User(name=name)
    db.session.add(user)
    return user
```

</td>
<td markdown="block">

```python
def add_user(session: Session, name: str) -> User:
    user = User(name=name)
    session.add(user)
    return user
```

The session is a parameter, of this function and of every function between
it and the block that opened the session.

</td>
</tr>

<tr>
<td markdown="block">Called from a job</td>
<td markdown="block">

```python
@db.transaction
def import_users(names: list[str]) -> None:
    for name in names:
        add_user(name)  # joins the job's transaction
```

</td>
<td markdown="block">

```python
def import_users(names: list[str]) -> None:
    with SessionFactory.begin() as session:
        for name in names:
            add_user(session, name)
```

</td>
</tr>

<tr>
<td markdown="block">Called on its own</td>
<td markdown="block">

```python
add_user("ada")  # a transaction of its own
```

</td>
<td markdown="block">

```python
with SessionFactory.begin() as session:
    add_user(session, "ada")
```

Every caller opens the transaction. A `SessionFactory.begin()` inside
`add_user()` itself would take a second connection when called from the job,
blind to the job's writes and able to deadlock on its rows.

</td>
</tr>

<tr>
<td markdown="block">Outside any block</td>
<td markdown="block">

```python
db.session  # MissingSessionError
```

</td>
<td markdown="block">

```python
SessionFactory().scalars(select(User))  # nothing closes this session
```

</td>
</tr>

<tr>
<td markdown="block">No transaction, every statement commits</td>
<td markdown="block">

```python
with db.autocommit():
    ...
```

</td>
<td markdown="block">

```python
with (
    engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn,
    Session(conn) as session,
):
    ...
```

</td>
</tr>

<tr>
<td markdown="block">Retried on a serialization failure</td>
<td markdown="block">

```python
@db.transaction(retry_on=is_conflict, max_retries=3)
def transfer(from_id: int, to_id: int, amount: int) -> None: ...
```

</td>
<td markdown="block">

```python
for attempt in range(4):
    try:
        with SessionFactory.begin() as session:
            transfer(session, from_id, to_id, amount)
        break
    except DBAPIError as error:
        if not is_conflict(error) or attempt == 3:
            raise
        sleep(backoff(attempt))
```

</td>
</tr>

<tr>
<td markdown="block">Async</td>
<td markdown="block">

```python
from sqlakit.asyncio import Database

db = Database(url)


@db.transaction
async def import_users(names: list[str]) -> None:
    for name in names:
        await add_user(name)
```

</td>
<td markdown="block">

```python
engine = create_async_engine(url)
SessionFactory = async_sessionmaker(engine)


async def import_users(names: list[str]) -> None:
    async with SessionFactory.begin() as session:
        for name in names:
            await add_user(session, name)
```

</td>
</tr>

<tr>
<td markdown="block">A `FastAPI` handler</td>
<td markdown="block">

```python
@app.post("/users")
@db.transaction
def create_user(payload: UserCreate) -> User:
    return add_user(payload.name)
```

</td>
<td markdown="block">

```python
def get_session() -> Iterator[Session]:
    with SessionFactory.begin() as session:
        yield session


@app.post("/users")
def create_user(payload: UserCreate, session: Session = Depends(get_session)) -> User:
    return add_user(session, payload.name)
```

</td>
</tr>

<tr>
<td markdown="block">A background task, after the response</td>
<td markdown="block">

```python
@app.post("/users")
@db.transaction
async def create_user(payload: UserCreate, background: BackgroundTasks) -> User:
    background.add_task(notify)
    return await add_user(payload.name)


async def notify() -> None:
    async with db.transaction():  # the handler's block has closed
        ...
```

</td>
<td markdown="block">

```python
@app.post("/users")
async def create_user(
    payload: UserCreate,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> User:
    background.add_task(notify, session)
    return await add_user(session, payload.name)


async def notify(session: AsyncSession) -> None:
    await session.execute(select(User))  # the session was closed after the response
```

A closed session opens a new connection on its next use, in a transaction
nobody commits, with no error.

</td>
</tr>

<tr>
<td markdown="block">A test that rolls back</td>
<td markdown="block">

```ini
# pytest.ini
[pytest]
sqlakit = true
```

```python
# conftest.py
@pytest.fixture(scope="session")
def sqlakit_db() -> Database:
    return db


@pytest.fixture(scope="session")
def sqlakit_metadata() -> sa.MetaData:
    return Base.metadata
```

```python
@pytest.mark.db
def test_a_user_is_saved() -> None:
    add_user("ada")
```

</td>
<td markdown="block">

```python
@pytest.fixture(scope="session", autouse=True)
def schema() -> Iterator[None]:
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def session() -> Iterator[Session]:
    conn = engine.connect()
    transaction = conn.begin()
    session = Session(conn, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    transaction.rollback()
    conn.close()


def test_a_user_is_saved(session: Session) -> None:
    add_user(session, "ada")
```

A handler under test gets the session through `app.dependency_overrides`.

</td>
</tr>

</tbody>
</table>
