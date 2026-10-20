# py-autowired

[![CI](https://github.com/AyhanOztemel/py-autowired/actions/workflows/tests.yml/badge.svg)](https://github.com/AyhanOztemel/py-autowired/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/py-autowired.svg)](https://pypi.org/project/py-autowired/)

py-autowired automatically fills runtime dependency fields declared as
typed class fields ending in `_instance` and initialized to `None`.
Register services once, then enable autowiring at startup:

```python
from py_autowired import Container, auto_inject

class ProductRepository:
    def list_products(self):
        return ["Notebook", "Pencil"]

class ProductService:
    product_repository_instance: ProductRepository | None = None

    def list_products(self):
        return self.product_repository_instance.list_products()

class ProductController:
    product_service_instance: ProductService | None = None

    def list_products(self):
        return self.product_service_instance.list_products()

container = Container()
container.register_scoped(ProductRepository)
container.register_transient(ProductService)

auto_inject(container, root_dir="/absolute/path/to/project")
with container.create_scope():
    print(ProductController().list_products())
```

Autowiring preserves explicit non-`None` assignments. Unmatched fields remain
`None`. Service selection and lifetime rules are the same for class fields and
assignments inside `__init__`.
Type annotations help readers and tools; selection still uses the **field name**
and module proximity, not the annotation. Keep the `= None` default: an annotation
alone does not initialize an attribute. `self.<name>_instance = None` remains
supported. Since the type includes `None`, type checkers may require a guard or
assertion before dereferencing an injected field.

## Validate at startup

Validation is **off by default**. After registering services, enable it explicitly:

```python
auto_inject(container, root_dir="/absolute/path/to/project", validate=True)
```

For FastAPI use `attach_container(app, container, root_dir=..., validate=True)`;
the check runs during lifespan startup. To check again after registrations change:

```python
from py_autowired import AutowiringValidationError, validate_autowiring

try:
    validate_autowiring(container)
except AutowiringValidationError as error:
    for issue in error.issues:
        print(issue)
    raise
```

The check reads fields of classes patched for that container and collects missing
matches, equally ranked candidates, and statically unresolvable registrations in
one exception. Repeated alias/index entries for the same registration are counted
once. Module proximity can select a unique winner; tied runtime candidates still
use the original registration order when validation is disabled.

The validation check does not construct objects/services, call factories or field
descriptors, or open scopes, and leaves runtime lookup caches untouched. It may
evaluate type annotations, which can execute user code with side effects.
Required constructor dependencies, abstract implementations, cycles, and declared `factory_dependencies`
are inspected. Arbitrary constructor/factory bodies, custom setters, and factory
dependencies without metadata cannot be proven safe statically. Constructor-time
manual overrides cannot be inferred, so candidate fields there are checked
conservatively; concrete class-level overrides are skipped. The standalone check
does not discover or patch new classes. A failed `auto_inject(..., validate=True)`
leaves its already-installed patches in place; abort startup or correct the graph.

Validation also reports required untyped constructor parameters and singleton
capture of scoped dependencies through constructors, injected fields, and declared
factory metadata, including indirect captures. Runtime lifetime behavior is unchanged.
Inherited unannotated class fields are not added to a subclass's post-init injection
candidates; validation logs this as an informational WARNING (not a failure), even for discovered subclasses
skipped by patching. A base constructor or explicit assignment may still fill them.

Tied candidates keep the registration-order winner and are reported only by
opt-in validation (no runtime log). Field-read
failures emit WARNING with traceback and still skip the field. Discovery warns
when an imported module's file differs from the expected file, and does not count
that import as successful or replace the existing module.

`resolve()` is synchronous: a factory that returns a bare coroutine raises
`AsyncFactoryError` (the coroutine is closed) instead of injecting it. Other
awaitable objects returned by factories are services and are left unchanged.
When scope disposal fails more than once, the first error is re-raised with its
original type; the others are attached as notes and logged.

**Choosing between candidates:** the field name already acts as a qualifier
(`utc_clock_instance` selects `UtcClock`). When several registrations still match,
mark the preferred one with `container.set_primary(UtcClock)` (like Spring's
`@Primary`); without a mark the module-proximity rule decides as before.
Several implementations of **one** type can be registered under keys:

```python
from typing import Annotated
from py_autowired import Keyed

container.register_keyed(IClock, "utc", UtcClock)
container.register_keyed(IClock, "local", LocalClock)

class Report:
    def __init__(self, clock: Annotated[IClock, Keyed("utc")]): ...
```

Keyed registrations do not take part in `*_instance` field matching.

**Open generics (opt-in):** one registration serves every closed type:

```python
container.register_open_generic(Repository, SqlRepository)   # class SqlRepository(Repository[T])
users = container.resolve(Repository[User])                   # a SqlRepository for User
```

**Deferred resolution (opt-in):** annotate a constructor parameter with
`Provider[T]` (resolve on every call) or `Lazy[T]` (resolve once on first `.value`):

```python
from py_autowired import Lazy, Provider

class ReportService:
    def __init__(self, sessions: Provider[Session], exporter: Lazy[PdfExporter]):
        self.sessions = sessions      # self.sessions() -> Session of the current scope
        self.exporter = exporter      # self.exporter.value -> built on first use
```

A singleton may safely hold `Provider[ScopedService]`: each call resolves from the
scope that is active at that moment.

**Shutdown (opt-in):** after application work stops, call `container.close()` or
`await container.aclose()`. Container-created singletons are disposed in reverse
creation order (`dispose`/`close`, or `aclose` in async). Objects passed to
`register_instance` belong to you and are not disposed. Afterwards `resolve()`
raises `ContainerClosedError`.

**Concurrency:** ready singletons and cached lookups are lock-free. A dependency
cycle raises `CircularDependencyError` even when two threads first-resolve the
cyclic singleton graph at the same time (no deadlock). `refresh()` never leaves a
stale cached choice, and closing a scope disposes services still being created.
Detect cycles at startup with `auto_inject(..., validate=True)`.

## Testing: swap a service

Given the classes above, a pytest test can swap the repository while keeping
automatic field injection:

```python
from py_autowired import Container, auto_inject

class FakeProductRepository(ProductRepository):
    def list_products(self):
        return ["Test product"]

def test_products():
    container = Container()
    container.register_transient(ProductRepository)
    container.register_transient(ProductService)
    container.replace(ProductRepository, FakeProductRepository)
    auto_inject(container, root_dir="/absolute/path/to/project", validate=True)
    assert ProductController().list_products() == ["Test product"]
```

Use a fresh container per test. If replacing a registration **after** autowiring
has started, call `refresh()` before constructing new consumers. Already-created
consumers keep their existing dependency objects. Run tests using different active
containers sequentially; autowiring has one process-wide active container.

**Known limitation:** after `replace()`, an alias still points to the old registration.

## Compatibility

Measured by `tests/test_field_compatibility.py` on Python 3.11 and Pydantic 2:

| Class form | Observed automatic field injection |
| --- | --- |
| Ordinary typed class, `field_instance: Service \| None = None` | Supported; non-`None` assignments preserved. |
| Mutable `dataclass`, with or without `slots=True` | Supported, including explicit constructor overrides. Apply autowiring after the dataclass decorator. |
| `__slots__` | Supported when the slot exists and is initialized to `None` in `__init__`; an uninitialized slot stays unset. |
| Frozen dataclass, with or without slots | Unsupported: post-init assignment raises `AutoInjectionResolutionError` with the original immutability error as cause. |
| Pydantic `BaseModel` v2 | Mutable models work with `arbitrary_types_allowed=True` for service types. Injection happens after model validation and `model_post_init`; those hooks cannot rely on filled fields. Injected values do not receive a new full model validation pass. |
| Frozen Pydantic model | Unsupported: assignment fails and is wrapped in `AutoInjectionResolutionError`. |
| `attrs` | Optional tests cover mutable/slotted and frozen forms, but skipped locally because `attrs` is absent; compatibility is unverified here. |

These results do not guarantee compatibility with custom descriptors, setters,
Pydantic v1, or every third-party class configuration. Validation does not make
immutable classes writable.

## Test quality

The suite runs on CPython 3.10, 3.11, 3.14 and the free-threaded 3.14t build
(GIL disabled). The injection engine and the container were mutation tested
with cosmic-ray: 1399 mutants, **99.3% killed** among mutants that can change
behavior (88.0% of all mutants; the rest are proven equivalent, each with its
reason). The 9 mutants that are neither killed nor proven equivalent are listed
openly. Full report: [docs/MUTATION_REPORT.md](https://github.com/AyhanOztemel/py-autowired/blob/main/docs/MUTATION_REPORT.md).

## Logging

The library uses `logging.getLogger("py_autowired")`, with English messages and
a `NullHandler` by default. Configure application logging (for example
`logging.basicConfig(level=logging.WARNING)`) to display discovery failures;
failed imports always emit WARNING records and do not raise. No Python
`warnings.warn()` calls are added. Set `PY_AUTOWIRED_DEBUG=1` (or the existing
`DI_DEBUG=1` fallback) **before import** for DEBUG messages and a library-owned
console `StreamHandler` on stderr. Normal operation writes nothing to stdout.
The legacy `print_cache_stats()` remains available and writes to stdout (unchanged).

## Field injection benchmark

Run locally, without network access or extra packages:

```bash
python -B benchmarks/field_injection.py --number 100000 --repeat 5
```

Example on Windows AMD64, Python 3.11.0 (best of five, 100,000 creations each):
plain object **0.227 µs/object**; automatic field injection **2.721 µs/object**;
additional time **2.494 µs/object**, **11.97×** total time. Results vary by machine,
Python version, and dependency graph. This compares
one ordinary attribute assignment with one injected field using an already-created
singleton and warm caches. Startup discovery, validation, and service construction
are excluded; it is a local measurement, not a performance guarantee.

## FastAPI in a few lines

Install `py-autowired[fastapi]`. With the classes and registrations above,
replace the standalone `auto_inject()` and scope block with this setup:

```python
from fastapi import FastAPI
from py_autowired.fastapi import attach_container

app = FastAPI()
```

```python
attach_container(app, container, root_dir="/absolute/path/to/project")
@app.get("/products")
def products(): return ProductController().list_products()
```

Startup installs automatic field injection and preserves the app's existing
lifespan. Every HTTP request gets its own scope, shared by sync and async
endpoints and dependencies in that request. Concurrent requests have separate
scoped objects. Sync and async finalizers are completed when the request ends,
including streaming and background work. Cleanup is shielded from active AnyIO
cancellation. Construct controllers normally inside endpoints; their fields are
filled automatically.

All user middleware and exception handlers, including `Exception`/500 handlers,
share the open request scope and can use automatic request-scoped injection.
The scope closes after the response and background work finish.

For optional **FastAPI-style parameter injection**, use
`controller=Depends(injected(ProductController))` after importing `Depends` from
`fastapi` and `injected` from `py_autowired.fastapi`. Registered types respect
their lifetime; unregistered controllers are constructed without arguments.

See [the runnable FastAPI example](examples/fastapi_app/app.py).

## Lifetime and safety notes

Only **one active autowiring container per process** is supported. Class patches
and the active name map are process-wide. `activate_container()` switches the
active graph for controlled, sequential reuse; simultaneous apps with different
containers must run in separate processes. Request scopes within one container
are isolated through context variables.

**Lifetime guidance:** A singleton should not retain a scoped service in a field;
use a scoped owner so it does not retain request resources after their scope
closes. This is documentation guidance only; runtime behavior is unchanged.

`auto_inject()` imports modules under its discovery root. Use a trusted project
directory and `exclude_dirs` for directories that should not be imported.

## Runtime package

```text
py_autowired/
  __init__.py
  autowired.py
  container.py
  fastapi.py     # optional integration; imported explicitly
  py.typed
```

`__init__.py` exports the public core API. The core has no framework dependency;
FastAPI is imported only by the optional bridge module.

## Frameworks

Console, FastAPI, Flask and Django examples live under `examples/`. Their
controllers and services use automatic `*_instance` field injection.

See [KULLANIM_KILAVUZU_TR.md](KULLANIM_KILAVUZU_TR.md) and
[USAGE_GUIDE_EN.md](USAGE_GUIDE_EN.md).
