# py_autowired with Flask

A small but complete Flask project. Run it first, then read the files in the
order of the folder list below.

## Run

```bash
pip install -r requirements.txt
python app.py          # or: flask --app app run --port 8002
```

Then open http://127.0.0.1:8002/products

## The idea in three rules

1. **Classes do not import each other.** A class only declares what it needs,
   for example `self.product_service_instance = None`. The field name says
   which contract is meant: `product_service_instance` is an
   `IProductService`, `product_repository_instance` is an
   `IProductRepository`. The fields are filled automatically at runtime.
2. **`register/` is the only place with imports.** It says which class to
   create for each need and how long each object lives.
3. **`bootstrap/` brings the application up:** it creates the container, adds
   the registrations and connects Flask. Your entry file calls it once:
   `bootstrap(app)`.

## Folder layout

```text
domain/                 # contracts only (interfaces), no code
  product_repository.py # IProductRepository
  clock.py              # IClock
  product_service.py    # IProductService
infrastructure/         # implementations of the contracts
  memory_product_repository.py
  system_clock.py
application/            # business logic
  product_service.py    # implements IProductService; needs a repository and a clock
presentation/           # what the user calls
  product_controller.py # needs an IProductService - imports nothing
  routes.py             # the HTTP endpoints (a Blueprint)
register/               # THE ONLY PLACE THAT IMPORTS THE CLASSES
  register.py           # which class, how long it lives
bootstrap/              # how the application comes up
  bootstrap.py          # container + registrations + the Flask bridge
app.py                  # creates the app and calls bootstrap(app)
requirements.txt        # also marks the project root
```

The only other import between layers: an implementation imports the
contract it implements (`MemoryProductRepository(IProductRepository)`,
`ProductService(IProductService)`).

## register/register.py

```python
from application.product_service import ProductService
from domain.clock import IClock
from domain.product_repository import IProductRepository
from domain.product_service import IProductService
from infrastructure.memory_product_repository import MemoryProductRepository
from infrastructure.system_clock import SystemClock


def register(container):
    container.register_singleton(IClock, SystemClock)
    container.register_scoped(IProductRepository, MemoryProductRepository)
    container.register_transient(IProductService, ProductService)
```

## bootstrap/bootstrap.py

```python
from py_autowired import Container
from py_autowired.flask import attach_container

from register import register


def bootstrap(app):
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    attach_container(app, container)   # scan on the first request, scope per request, close at exit
```

| Lifetime | In this example | Meaning |
| --- | --- | --- |
| `register_singleton` | `IClock` → `SystemClock` | one object for the whole application |
| `register_scoped` | `IProductRepository` → `MemoryProductRepository` | one object per HTTP request; its `close()` runs at the end |
| `register_transient` | `IProductService` → `ProductService` | a new object every time one is needed |

`ProductController` is not registered: it only *receives* a dependency and you
create it yourself with `ProductController()`. Register a class when another
class needs it.

## Naming: which field gets which class

The field name, without `_instance`, is compared with the registered contract
and class names. Underscores and upper/lower case do not matter, and these
contract prefixes (and the `Impl` suffix) are understood:

| Contract name | Field that receives it |
| --- | --- |
| `IProductRepository`, `I_ProductRepository` | `product_repository_instance` |
| `ABSProductRepository`, `ABS_ProductRepository` | `product_repository_instance` |
| `AbsProductRepository`, `Abs_ProductRepository` | `product_repository_instance` |
| `absProductRepository`, `abs_ProductRepository` | `product_repository_instance` |
| `ProductRepositoryImpl` | `product_repository_instance` |

## Contracts only: `strict_interfaces=True`

The container in `bootstrap/` is created with `Container(strict_interfaces=True)`:
only a contract (an interface / abstract class) can be registered, always
together with its implementation. So every dependency in the project points to
an abstraction, never to a concrete class.

## The bridge

`attach_container` scans the project on the first request, gives every request its own scope (kept open while the response is sent), and closes the singletons when the process exits. Nothing else to configure.

## In your own project

1. Write your classes with `*_instance = None` fields; do not import them into each other.
2. Copy the `register/` and `bootstrap/` folders; change the imports and
   `register_*` lines in `register/register.py`.
3. Call `bootstrap(...)` once, as in `app.py`.

You can also register only the interface, for example
`container.register_scoped(IProductRepository)`: py_autowired then finds its
single implementation by itself. Writing the class explicitly, as above, keeps
everything visible in one place.

Optional features (validation at startup, `Provider`/`Lazy`, keyed services,
generics, ...) are explained in the main README under *Optional features*.
