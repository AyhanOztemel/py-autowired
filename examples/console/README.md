# py_autowired with Console

A small but complete Console project. Run it first, then read the files in the
order of the folder list below.

## Run

```bash
pip install -r requirements.txt
python main.py
```

## The idea in three rules

1. **Classes do not import each other.** A class only declares what it needs,
   for example `self.product_service_instance = None`. The field name says
   which contract is meant: `product_service_instance` is an
   `IProductService`, `product_repository_instance` is an
   `IProductRepository`. The fields are filled automatically at runtime.
2. **`register/` names only contracts.** It lists the contracts the
   application uses and how long each object lives. py_autowired finds the
   class that implements each contract by itself; no implementation class is
   imported anywhere.
3. **`bootstrap/` brings the application up:** it creates the container, adds
   the registrations and connects Console. Your entry file calls it once:
   `container = bootstrap()`.

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
register/               # which contracts are used - names no implementation class
  register.py           # contract + how long it lives
bootstrap/              # how the application comes up
  bootstrap.py          # container + registrations + the Console bridge
main.py                 # starts the program
requirements.txt        # also marks the project root
```

Imports between layers point only to `domain/`: an implementation imports the
contract it implements (`MemoryProductRepository(IProductRepository)`,
`ProductService(IProductService)`), and `register/` imports the contracts.
Nothing imports `infrastructure/` or `application/`.

## register/register.py

```python
from domain.clock import IClock
from domain.product_repository import IProductRepository
from domain.product_service import IProductService


def register(container):
    container.register_singleton(IClock)
    container.register_scoped(IProductRepository)
    container.register_transient(IProductService)
```

## bootstrap/bootstrap.py

```python
from py_autowired import Container, auto_inject

from register import register


def bootstrap() -> Container:
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    auto_inject(container)        # scans the project; *_instance fields are filled from now on
    return container
```

| Lifetime | Registered contract → class found automatically | Meaning |
| --- | --- | --- |
| `register_singleton` | `IClock` → `SystemClock` | one object for the whole application |
| `register_scoped` | `IProductRepository` → `MemoryProductRepository` | one object per unit of work (`with container.create_scope():` in `main.py`); its `close()` runs at the end |
| `register_transient` | `IProductService` → `ProductService` | a new object every time one is needed |

**More than one implementation?** If a contract has two implementations (say
`MemoryProductRepository` and `SqlProductRepository`), py_autowired does not
guess: it stops with `AmbiguousImplementationError`. Then name the one to use
as the second argument:
`container.register_scoped(IProductRepository, SqlProductRepository)`.

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
only a contract (an interface / abstract class) can be registered, never a
concrete class. So every dependency in the project points to an abstraction.

## The bridge

A console program has no requests, so there is no web bridge: `auto_inject(container)` turns injection on, and `main.py` opens one scope (`with container.create_scope():`) for one unit of work.

## In your own project

1. Write your classes with `*_instance = None` fields; do not import them into each other.
2. Copy the `register/` and `bootstrap/` folders; in `register/register.py`
   import your contracts and write one `register_*` line per contract.
3. Call `bootstrap(...)` once, as in `main.py`.


Optional features (validation at startup, `Provider`/`Lazy`, keyed services,
generics, ...) are explained in the main README under *Optional features*.
