# py-autowired English Usage Guide

## Core rule

Diagnostics details are in the README's startup-validation section. Validation
reports untyped required parameters, singleton-to-scoped captures (including declared
factory edges), and inherited unannotated fields excluded from subclass post-init
injection. Runtime field matching is unchanged. A factory returning a bare coroutine
raises `AsyncFactoryError`; other awaitables are returned unchanged. Ready
singletons and cached lookups are lock-free; a cross-thread singleton cycle raises
`CircularDependencyError` instead of blocking. Detect cycles at startup with
`validate=True`.

Declare every runtime dependency as a field ending in `_instance`:

```python
class OrderService:
    order_repository_instance: OrderRepository | None = None
```

`auto_inject()` matches `order_repository` with a registered
`IOrderRepository` or `OrderRepository` and assigns the object at runtime.
Application code does not resolve dependencies manually.
The repository type must already be imported (or use postponed annotations).
Annotations describe fields; matching still uses names and module proximity.
Keep `= None`: an annotation alone leaves the attribute unset. Assignments such
as `self.order_repository_instance = None` remain supported, and explicit
non-`None` values are preserved. Type checkers may need a guard before field use.

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

## Validate at startup

Validation is off by default. Use `auto_inject(container, root_dir=..., validate=True)`
or `attach_container(app, container, root_dir=..., validate=True)` for an opt-in
check (FastAPI runs it during lifespan startup). After patching, the standalone
`from py_autowired import validate_autowiring; validate_autowiring(container)`
checks again without discovering or patching classes.

`AutowiringValidationError.issues` collects missing matches, equally ranked
distinct registrations, and statically unresolvable types. Alias duplicates do
not count as ambiguity, and module proximity still selects unique winners.
The check does not construct objects/services, call factories or field descriptors,
open scopes, or update runtime lookup caches. It may evaluate type annotations,
which can execute user code with side effects. Checks include required constructor
dependencies, abstract implementations, cycles, and `factory_dependencies` metadata. Factory bodies,
arbitrary constructor code, and custom setters remain runtime concerns.
Constructor-time manual overrides cannot be inferred and are checked
conservatively; concrete class-level overrides are skipped. A validation failure
during `auto_inject` leaves patches installed: correct the graph or abort startup.

## Testing: swap a service

For `OrderService` above and an existing `OrderRepository`, this pytest recipe
keeps automatic injection:

```python
from py_autowired import Container, auto_inject

class FakeOrderRepository(OrderRepository):
    pass

def test_swap():
    container = Container()
    container.register_transient(OrderRepository)
    container.replace(OrderRepository, FakeOrderRepository)
    auto_inject(container, root_dir="/absolute/path/to/application", validate=True)
    service = OrderService()
    assert isinstance(service.order_repository_instance, FakeOrderRepository)
```

Use a fresh container per test. If replacement happens after autowiring, call
`from py_autowired import refresh; refresh()` before creating new consumers.
Existing objects keep their assigned dependencies. Different containers must be
used sequentially because the active autowiring container is process-wide.

## Compatibility

Measured on Python 3.11 and Pydantic 2.14.1 in
`tests/test_field_compatibility.py`:

| Form | Result |
| --- | --- |
| Typed ordinary class with `= None` | Works; explicit non-`None` values preserved. |
| Mutable dataclass, including `slots=True` | Works after the decorator; constructor overrides preserved. |
| `__slots__` | Works for an existing slot initialized to `None`; uninitialized slots stay unset. |
| Frozen dataclass (with/without slots) | Unsupported; post-init assignment fails with `AutoInjectionResolutionError`. |
| Mutable Pydantic v2 `BaseModel` | Works with `arbitrary_types_allowed=True`; fields fill after validation and `model_post_init`, without another full validation pass. |
| Frozen Pydantic v2 | Unsupported; assignment fails with `AutoInjectionResolutionError`. |
| `attrs` | Not installed locally; optional mutable/slotted/frozen tests skipped, so unverified. |

Original assignment errors are retained as exception causes. Custom descriptors,
setters, other model configurations, and Pydantic v1 are not covered by these
measurements. Static validation does not make immutable models writable.

## Logging and benchmark

Library messages are English records on `logging.getLogger("py_autowired")`.
Normally only a `NullHandler` is installed: configure application logging to see
discovery failures, which always log at WARNING without raising or using Python
warnings. Before import set `PY_AUTOWIRED_DEBUG=1` (`DI_DEBUG=1` fallback) for
DEBUG output and a library `StreamHandler` on stderr. Normal operation writes
nothing to stdout. `print_cache_stats()` writes to stdout (unchanged).

Run `python -B benchmarks/field_injection.py --number 100000 --repeat 5` locally.
Windows AMD64 / Python 3.11.0 example: 0.227 µs plain, 2.721 µs injected,
2.494 µs extra (11.97× total). These are best-of-five timings for one field and
a ready singleton with warm caches; startup and dependency construction are
excluded. Results vary by machine, Python version, and graph, with no performance
guarantee. See README for the full recipe and measured limitations.

## Lifetimes

- `register_singleton`: one object for the application lifetime.
- `register_scoped`: one object per active scope.
- `register_transient`: a new object per injection.
- `register_instance`: use an existing object.
- `register_factory`: create objects through `factory(container)`; the container
  is always passed as the single argument, even if the factory does not use it.

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

## Active container and lifetime guidance

Autowiring supports one active container per process. Class patches and the
active name map are process-wide. `activate_container(container)` switches an
already patched graph for controlled sequential reuse; it does not discover or
patch classes. Do not serve simultaneous applications with different containers
in the same process. Scopes within a single container use context variables and
remain isolated across concurrent requests.

**Lifetime guidance:** A singleton should not retain a scoped service in a field;
use a scoped owner so it does not retain request resources after their scope
closes. This is documentation guidance only; runtime behavior is unchanged.

Module discovery imports Python code. Point `root_dir` at trusted application
code, and use `exclude_dirs` to skip directories that must not be imported.

## FastAPI bridge

Install the optional `py-autowired[fastapi]` extra. Import `attach_container`
from `py_autowired.fastapi`; importing the core package does not load
FastAPI. After registrations, attach before starting the app:

```python
from fastapi import FastAPI
from py_autowired.fastapi import attach_container

class UserController:
    user_service_instance = None

    def execute(self):
        return self.user_service_instance.execute()

app = FastAPI()
attach_container(app, container, root_dir="/absolute/path/to/application")

@app.get("/users")
def users():
    return UserController().execute()
```

Attachment runs `auto_inject()` and `activate_container()` at startup and
preserves the existing application lifespan. Each HTTP request runs in
`async with container.create_scope()`, including streaming responses and
background tasks. Both synchronous and asynchronous finalizers are completed,
with cleanup shielded from active AnyIO cancellation.
WebSocket connections are outside this HTTP bridge's scope.

The request scope surrounds the entire Starlette middleware stack. All user
middleware and exception handlers, including `Exception`/500 handlers, share
the same open scope and can use automatic request-scoped injection. Cleanup
runs after the response and background work finish.

Optional FastAPI-style parameter injection uses
`controller=Depends(injected(UserController))`, with `Depends` imported from
`fastapi` and `injected` from `py_autowired.fastapi`.
`injected()` returns an automatically populated object. Registered types retain
their lifetime; unregistered controllers must be constructible without arguments.
Scoped services are shared within one request and differ across requests,
including concurrent requests. FastAPI's normal dependency caching still applies.
Run lifespan in tests with `with TestClient(app) as client:`.

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

Declare dependencies as `self.repository_instance = None` or a class field such
as `repository_instance = None`. Explicit non-`None` values are preserved, and
unmatched fields remain `None`.

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
