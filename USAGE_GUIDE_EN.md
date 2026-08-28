# py-autowired English Usage Guide

## Core rule

Declare every runtime dependency as a field ending in `_instance`:

```python
class OrderService:
    def __init__(self):
        self.order_repository_instance = None
```

`auto_inject()` matches `order_repository` with a registered
`IOrderRepository` or `OrderRepository` and assigns the object at runtime.
Application code does not resolve dependencies manually.

## Setup

```bash
pip install py-autowired
```

```python
from py_autowired.autowired import auto_inject
from py_autowired.container import Container

container = Container()
container.register_singleton(IClock, SystemClock)
container.register_scoped(IUserRepository, SqliteUserRepository)
container.register_transient(UserService)

# Exactly once during startup:
auto_inject(container, root_dir="/absolute/path/to/application")
```

Import implementations and complete registrations before `auto_inject()`.
Create injectable application objects only after that call.

## Lifetimes

- `register_singleton`: one object for the application lifetime.
- `register_scoped`: one object per active scope.
- `register_transient`: a new object per injection.
- `register_instance`: use an existing object.
- `register_factory`: create objects through a zero-argument factory.

Synchronous request or operation:

```python
with container.create_scope():
    result = UserController().execute()
```

Asynchronous request or operation:

```python
async with container.create_scope():
    result = await UserController().execute()
```

## Layered architecture

- Domain: repository/port abstractions.
- Infrastructure: concrete repositories/adapters.
- Application: services declaring `self.repository_instance = None`.
- Composition: registrations and the single `auto_inject()` call.
- Presentation: Console, FastAPI, Flask or Django controller/view.

Presentation and application layers contain no manual dependency-resolution calls. Runnable examples are available under `examples/`.

## Naming

Fields start with a lowercase letter and end with `_instance`:

- `user_repository_instance` → `IUserRepository`
- `payment_service_instance` → `PaymentService`
- `audit_instance` → `ABS_Audit`

Keep service names unique to avoid ambiguous name matching.

## Non-negotiable pattern

The only valid dependency declaration is: self.repository_instance = None.

## One-click run from IDLE

No terminal command is required. Open one of these entry-point files in IDLE and choose **Run > Run Module (F5)**:

- Console: `examples/console_app/main.py`
- FastAPI: `examples/fastapi_app/app.py` — `http://127.0.0.1:8101/di-demo/Ayhan`
- Flask: `examples/flask_app/app.py` — `http://127.0.0.1:8102/di-demo/Ayhan`
- Django: `examples/django_app/manage.py` — `http://127.0.0.1:8103/di-demo/Ayhan`

Install the optional framework dependencies listed in `examples/requirements.txt` once before running the web examples. No manual `PYTHONPATH` setting is needed when running from the source tree.

## Deep-folder proof

`examples/shared` is deliberately not flat. Domain contracts, use case, repository, formatter, runtime provider, and composition root live in separate nested folders. The working injection chain is:

`Controller -> GreetingService -> MemoryMessageRepository -> TurkishMessageFormatter -> RuntimeLabelProvider`

Every dependency in this chain is populated only from a `self.<name>_instance = None` declaration. The examples therefore verify that `auto_inject()` continues to work as folder and module depth increases.
