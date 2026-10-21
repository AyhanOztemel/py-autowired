# py_autowired

[![CI](https://github.com/AyhanOztemel/py-autowired/actions/workflows/tests.yml/badge.svg)](https://github.com/AyhanOztemel/py-autowired/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/py_autowired.svg)](https://pypi.org/project/py_autowired/)

**Runtime dependency injection for Python, without imports between your classes.**

A class only says what it needs:

```python
class ProductService:
    def __init__(self):
        self.product_repository_instance = None   # filled automatically
```

py_autowired fills `product_repository_instance` at runtime. `ProductService`
does not import the repository, does not receive it as a constructor argument,
and does not call any container. The classes stay independent of each other.
One `register/` folder lists the contracts they need; py_autowired finds the
class behind each contract by itself.

Türkçe: [KULLANIM_KILAVUZU_TR.md](KULLANIM_KILAVUZU_TR.md)

## Install

```bash
pip install py_autowired
```

## Quick start (FastAPI)

**1. Your classes** - they depend on contracts (interfaces / abstract
classes) and never import each other; an implementation imports only the
contract it implements:

```python
# application/product_service.py
from domain.product_service import IProductService

class ProductService(IProductService):
    def __init__(self):
        self.product_repository_instance = None   # -> IProductRepository

    def list_products(self):
        return self.product_repository_instance.list_products()

# presentation/product_controller.py
class ProductController:
    def __init__(self):
        self.product_service_instance = None      # -> IProductService

    def list_products(self):
        return self.product_service_instance.list_products()
```

**2. `register/register.py`** - lists the contracts and how long each object
lives. It names no implementation class: py_autowired finds the single class
that implements each contract:

```python
from domain.product_repository import IProductRepository
from domain.product_service import IProductService


def register(container):
    container.register_scoped(IProductRepository)    # finds MemoryProductRepository
    container.register_transient(IProductService)    # finds ProductService
```

**3. `bootstrap/bootstrap.py`** - how the application comes up:

```python
from py_autowired import Container
from py_autowired.fastapi import attach_container

from register import register


def bootstrap(app):
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    attach_container(app, container)
```

**4. `main.py`** - one call:

```python
from fastapi import FastAPI
from bootstrap import bootstrap
from presentation.product_controller import ProductController

app = FastAPI()
bootstrap(app)

@app.get("/products")
def products():
    return ProductController().list_products()   # fields are already filled
```

Run it with `uvicorn main:app` or `python main.py`. There is no `root_dir`, no
lifespan and no middleware to write: the project is scanned when FastAPI starts
(in its lifespan), every request gets its own scope, and singletons are closed
when FastAPI stops. The complete project is in [examples/fastapi](examples/fastapi).

## The three rules

1. **Declare, do not import.** A field ending in `_instance` and set to `None`
   is a dependency. Its name selects the contract: `product_service_instance`
   means `IProductService` (see *Naming*). A value you assign yourself is
   never overwritten; a field with no matching registration stays `None`.
2. **Register in one place.** `register/` imports the classes and says which
   class to create for each need and how long it lives.
3. **Bootstrap once.** `bootstrap/` creates the container, adds the
   registrations and connects the framework with one bridge call (see
   *Frameworks*); the entry file calls `bootstrap(...)` once.

Type annotations are optional (`product_service_instance: IProductService | None = None`
also works). They help your editor, but they need an import; the name alone is
enough for py_autowired.

## Naming: which field gets which class

The field name without `_instance` is compared with the registered contract and
class names. Underscores and upper/lower case do not matter, and these contract
prefixes (and the `Impl` suffix) are understood:

| Contract name | Field that receives it |
| --- | --- |
| `IProductService`, `I_ProductService` | `product_service_instance` |
| `ABSProductService`, `ABS_ProductService` | `product_service_instance` |
| `AbsProductService`, `Abs_ProductService` | `product_service_instance` |
| `absProductService`, `abs_ProductService` | `product_service_instance` |
| `ProductServiceImpl` | `product_service_instance` |
| `ProductService` (no contract) | `product_service_instance` |

A prefix counts only when the next letter is upper case (or an underscore
follows), so names such as `Absolute` or `Ignore...` stay ordinary words.

**How a field may be spelled.** Underscores and upper/lower case in the field
name do not matter either. These four fields all receive `SqlProductRepository`:

```python
self.sql_product_repository_instance = None    # Python style (recommended)
self.sqlproductrepository_instance = None
self.sqlProductRepository_instance = None      # Java / C# style
self.SqlProductRepository_instance = None
```

Only the ending is fixed: `_instance` receives one object, `_instances` receives
every implementation of a contract as a list (see *Several implementations of
one contract*).

## Registering services: `container.register_*`

| Call | Lifetime | Use it for |
| --- | --- | --- |
| `container.register_singleton(IClock)` | one object for the whole application | configuration, clocks, clients that are safe to share |
| `container.register_scoped(IRepo, SqlRepo)` | one object per scope (a web request) | database sessions, unit of work |
| `container.register_transient(IProductService)` | a new object every time | stateless services |
| `container.register_instance(Settings, settings)` | the object you give | objects you created yourself; never closed by py_autowired |

- `register_*(Contract)` - the form used in every example: py_autowired finds
  the single implementation of the contract by itself, also when it lives in
  another layer.
- `register_*(Contract, Implementation)` - needed when a contract has more than
  one implementation. Without it py_autowired does not guess; it stops with
  `AmbiguousImplementationError`. Register the contract once per class to use
  several of them (see *Several implementations of one contract*).
- `register_*(ConcreteClass)` - the class is its own implementation. Not allowed
  with `Container(strict_interfaces=True)`.
- **Contracts only:** `Container(strict_interfaces=True)` (used in every example)
  accepts only interfaces / abstract classes as the registered type. Then every
  dependency in the project points to an abstraction, never to a concrete class.
- Keep the `register_*` lines in `register/`. Then no file imports an
  implementation class: imports point only to the contracts.
- Register a class when **another class needs it**. A class that only receives
  dependencies (a controller) is created normally: `ProductController()`.
- Scoped objects with a `close()`/`dispose()` (or `aclose()`) method are closed
  when their scope ends. Container-created singletons are closed when the
  application stops (web bridges) or when you call `container.close()`.

## Frameworks

Every example is a complete project with the same layers and its own README.

| Framework | Bridge call (in `bootstrap/`) | Scope | Example |
| --- | --- | --- | --- |
| Console | `auto_inject(container)` | `with container.create_scope():` | [examples/console](examples/console) |
| FastAPI | `attach_container(app, container)` | every HTTP request | [examples/fastapi](examples/fastapi) |
| Flask | `attach_container(app, container)` | every HTTP request | [examples/flask](examples/flask) |
| Django | `application = attach_container(application, container)` | every HTTP request | [examples/django](examples/django) |

Import the bridge from `py_autowired.fastapi`, `py_autowired.flask` or
`py_autowired.django`. Each bridge finds the project folder by itself (the
folder with `requirements.txt`, `pyproject.toml`, `.git`, `main.py`/`app.py`, or
`manage.py` for Django), scans it once, opens a scope for every request, and
closes singletons when the application stops.

## Optional features

None of these is needed for the setup above. Each one solves a specific
problem; use it only when you have that problem.

### Validate at startup

**Why:** a missing registration normally shows up only when the field is used.
**Benefit:** all wiring problems are reported at startup, in one error.
**How:**

```python
attach_container(app, container, validate=True)   # any bridge
auto_inject(container, validate=True)             # console
```

It lists fields with no matching registration, equally good candidates,
constructor dependencies that cannot be resolved, dependency cycles, and a
singleton that would capture a scoped service. It only reads; it constructs no
services. To check again later: `validate_autowiring(container)`
(raises `AutowiringValidationError` with `.issues`). A file that could not be
loaded during discovery is named on a line of its own and checked when it loads
(see [Files loaded after startup](#files-loaded-after-startup)).

### `Provider[T]` and `Lazy[T]`

**Why:** sometimes an object must not be created up front - it is expensive,
or it belongs to a request while the holder lives longer.
**Benefit:** create it later, at the moment it is needed.
**How** (constructor parameters):

```python
from py_autowired import Lazy, Provider

class ReportService:
    def __init__(self, sessions: Provider[Session], exporter: Lazy[PdfExporter]):
        self.sessions = sessions    # self.sessions() -> the Session of the current scope
        self.exporter = exporter    # self.exporter.value -> created on first use, then reused
```

A singleton may safely hold `Provider[ScopedService]`: every call resolves from
the scope active at that moment.

### Several implementations of one contract

**Why:** one contract, several classes used side by side - for example a
memory and a SQL repository, or several notification channels.
**Benefit:** as in .NET (`IEnumerable<T>`) and Spring (`List<T>`), every
implementation is available - still through `*_instance` fields, with no
imports and no constructor parameters.
**How:** register the contract once for each class:

```python
# register/register.py
container.register_scoped(IProductRepository, MemoryProductRepository)
container.register_scoped(IProductRepository, SqlProductRepository)
```

```python
# application/report_service.py - imports nothing
class ReportService:
    def __init__(self):
        self.memory_product_repository_instance = None  # -> MemoryProductRepository
        self.sql_product_repository_instance = None     # -> SqlProductRepository
        self.product_repository_instance = None         # -> SqlProductRepository (the last one)
        self.product_repository_instances = None        # -> [MemoryProductRepository, SqlProductRepository]

    def count(self):
        return sum(len(repo.list_products()) for repo in self.product_repository_instances)
```

| Field | Receives |
| --- | --- |
| named after a class (`sql_product_repository_instance`) | that class |
| named after the contract (`product_repository_instance`) | the last registered class - as before, and as `resolve()` |
| contract name + `_instances` (`product_repository_instances`) | all of them as a list, in registration order |

- Each registration keeps its own lifetime (singleton, scoped or transient).
- Registering the same class again does not add a second copy;
  `replace()` removes all earlier registrations of the contract.
- In code without fields: `container.resolve_all(IProductRepository)` returns
  the same list.
- `register_scoped(IProductRepository)` alone, without a class, still stops
  with `AmbiguousImplementationError` when two implementations exist:
  py_autowired does not guess which one you mean.
- `Container(strict_registrations=True)` forbids registering a contract twice,
  so this feature is not available there.

### Choosing between several implementations: `set_primary`

**Why:** two registered classes both match a field name.
**Benefit:** decide the winner once instead of renaming fields.
**How:** `container.set_primary(UtcClock)` (like Spring's `@Primary`). Without
it, the class closest to the consumer's module wins.

### Keyed services

**Why:** one interface, several implementations used side by side
(for example a UTC and a local clock).
**Benefit:** pick the implementation by a key.
**How:**

```python
from typing import Annotated
from py_autowired import Keyed

container.register_keyed(IClock, "utc", UtcClock)
container.register_keyed(IClock, "local", LocalClock)

class Invoice:
    def __init__(self, clock: Annotated[IClock, Keyed("utc")]): ...
```

Keyed registrations are used through constructors and `resolve`, not through
`*_instance` fields. With fields, use *Several implementations of one contract*:
there the class name works as the key.

### Open generics

**Why:** many types need the same kind of service (`Repository[User]`,
`Repository[Order]`, ...).
**Benefit:** one registration serves all of them.
**How:**

```python
container.register_open_generic(Repository, SqlRepository)   # class SqlRepository(Repository[T])
```

Closed generics are used through constructors and `resolve`, not through
`*_instance` fields.

### Closing the container yourself

**Why:** a console program or script with no bridge should release resources.
**Benefit:** singletons are disposed in reverse creation order.
**How:** `container.close()` or `await container.aclose()` when the work is
done. The framework bridges already do this when the application stops.
Objects given to `register_instance` are never closed. After `close()`,
`resolve()` raises `ContainerClosedError`.

### Swapping a service in tests

**Why:** a test should use a fake instead of the real database.
**Benefit:** the code under test stays unchanged.
**How:**

```python
container.replace(IProductRepository, FakeProductRepository)
```

Use a fresh container per test. If you replace a registration after
autowiring has started, call `refresh()` before creating new consumers.

### FastAPI parameter style

**Why:** you prefer FastAPI's `Depends` for some endpoints.
**How:** `controller=Depends(injected(ProductController))`, with `injected`
from `py_autowired.fastapi`. The fields of the object are filled the same way.

### Logging and debug output

The library logs to `logging.getLogger("py_autowired")` and is silent by
default. Discovery problems are logged as WARNING. Set `PY_AUTOWIRED_DEBUG=1`
before import for detailed DEBUG output.

### Files loaded after startup

A project file that imports the application module while that module is
still starting (for example `main.py` doing `from app import app`) cannot be
loaded during discovery. It is not lost, and nothing about it is hidden:

- startup logs it at INFO: `Skipped main during discovery: ... it is checked when it loads`;
- the moment it loads, its `*_instance` fields are filled **and checked** the
  way `validate=True` checks them. A problem is logged as ERROR at once (with
  `validate=True` the import of that file fails with `AutowiringValidationError`),
  so it never waits until the field is used. A healthy file logs INFO
  `Checked main after it loaded ...`;
- `skipped_during_discovery()` returns the names, so a test can load them:

```python
import importlib
from py_autowired import skipped_during_discovery

def test_files_skipped_at_startup_load_and_are_wired():
    import app                                   # the application starts
    for name in skipped_during_discovery():
        importlib.import_module(name)            # checked now (validate=True: raises on a problem)
```

`validate_autowiring(container)` also checks a skipped file that has loaded
since, and names the ones that have not loaded yet on a line of their own.

A skipped file that is **never imported** is never checked: it stays an INFO
line and a name in `skipped_during_discovery()`. If your server starts from the
application module itself, import the skipped files in a test as above. If a
file finishes loading in another thread while the library is busy, its import
cannot be failed any more: the problem is logged as ERROR at once and, with
`validate=True`, the next `auto_inject()` or `validate_autowiring()` raises it.

## Notes

- **Concurrency:** ready singletons and cached lookups are lock-free. A
  dependency cycle raises `CircularDependencyError` instead of freezing, even
  when two threads hit it at the same time. The suite runs on the free-threaded
  CPython 3.14t build.
- **One container per process:** class patches are process-wide; run separate
  applications in separate processes.
- **Discovery imports modules** under the project folder. Use `exclude_dirs` for
  folders that must not be imported. `tests` and `examples` folders inside the
  project are skipped automatically.
- **`resolve()` is synchronous:** a factory that returns a bare coroutine raises
  `AsyncFactoryError`.
- **Compatibility:** ordinary classes, `__slots__`, mutable dataclasses and
  mutable Pydantic v2 models (with `arbitrary_types_allowed=True`) work. Frozen
  dataclasses and frozen Pydantic models cannot receive fields (an
  `AutoInjectionResolutionError` explains why).
- **Speed:** an injected field costs a few microseconds per object creation on
  warm caches; measure on your machine with
  `python -B benchmarks/field_injection.py --number 100000 --repeat 5`.

## Test quality

The suite runs on CPython 3.10, 3.11, 3.14 and the free-threaded 3.14t build
(GIL disabled). The injection engine and the container were mutation tested
with cosmic-ray: 1399 mutants, **99.3% killed** among mutants that can change
behavior (88.0% of all mutants; the rest are proven equivalent, each with its
reason). The 9 mutants that are neither killed nor proven equivalent are listed
openly. The code added in 0.5.1 (bridges, late imports, prefixes) was tested
the same way: 329 mutants, **99.0%** killed among those that can change
behavior; the code changed in 0.5.2 (several implementations per contract,
upper case field names): 216 mutants, **100%** of those that can change
behavior killed. Full report: [docs/MUTATION_REPORT.md](https://github.com/AyhanOztemel/py-autowired/blob/main/docs/MUTATION_REPORT.md).

## License

MIT
