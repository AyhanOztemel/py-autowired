"""0.5.1: the simple setup works in a real layered project.

- Registering only the interface finds its implementation even when the
  implementation lives in another layer that is not imported yet.
- attach_container(app, container) needs no root_dir, also when the server's
  __main__ is not the application (``uvicorn main:app``).
- attach_container disposes container-created singletons when the app shuts
  down, and a later startup gets fresh ones.
"""

import abc
import importlib
import sys
import types

import pytest

from py_autowired import AmbiguousImplementationError, Container, ResolutionError, autowired


# ---- deferred implementation lookup -------------------------------------------
def test_interface_registration_waits_for_an_implementation_defined_later():
    class IStore(abc.ABC):
        @abc.abstractmethod
        def get(self): ...

    container = Container()
    container.register_singleton(IStore)              # nothing implements it yet

    class MemoryStore(IStore):                        # e.g. imported later by discovery
        def get(self):
            return "memory"

    assert container.resolve(IStore).get() == "memory"
    assert container.registrations[IStore].implementation_type is MemoryStore


def test_missing_implementation_is_reported_when_resolving():
    class INothing(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container = Container()
    container.register_transient(INothing)
    with pytest.raises(ResolutionError, match="No imported concrete implementation found for INothing"):
        container.resolve(INothing)


def test_two_implementations_found_later_are_ambiguous():
    class IPair(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container = Container()
    container.register_transient(IPair)

    class A(IPair):
        def run(self): ...

    class B(IPair):
        def run(self): ...

    with pytest.raises(AmbiguousImplementationError, match="IPair"):
        container.resolve(IPair)


# ---- a real layered project ------------------------------------------------------
LAYERS = ("domain", "infrastructure", "application", "presentation", "bootstrap", "main")

FILES = {
    "domain/contracts.py": (
        "import abc\n"
        "class IProductRepository(abc.ABC):\n"
        "    @abc.abstractmethod\n"
        "    def list_products(self): ...\n"),
    "infrastructure/memory_product_repository.py": (
        "from domain.contracts import IProductRepository\n"
        "class MemoryProductRepository(IProductRepository):\n"
        "    closed = []\n"
        "    def list_products(self):\n"
        "        return ['Notebook', 'Pencil']\n"
        "    def close(self):\n"
        "        MemoryProductRepository.closed.append(self)\n"),
    "application/product_service.py": (
        "class ProductService:\n"
        "    def __init__(self):\n"
        "        self.product_repository_instance = None\n"
        "    def list_products(self):\n"
        "        return self.product_repository_instance.list_products()\n"),
    "presentation/product_controller.py": (
        "class ProductController:\n"
        "    def __init__(self):\n"
        "        self.product_service_instance = None\n"
        "    def list_products(self):\n"
        "        return self.product_service_instance.list_products()\n"),
    "bootstrap/composition.py": (
        "from py_autowired import Container\n"
        "from application.product_service import ProductService\n"
        "from domain.contracts import IProductRepository\n"
        "container = Container()\n"
        "container.register_singleton(IProductRepository)\n"
        "container.register_transient(ProductService)\n"),
}

FASTAPI_MAIN = (
    "from fastapi import FastAPI\n"
    "from py_autowired.fastapi import attach_container\n"
    "from bootstrap.composition import container\n"
    "from presentation.product_controller import ProductController\n"
    "app = FastAPI()\n"
    "attach_container(app, container)\n"
    "@app.get('/products')\n"
    "def products():\n"
    "    return ProductController().list_products()\n")


def _forget_project_modules():
    for name in [n for n in sys.modules if n.split(".")[0] in LAYERS]:
        sys.modules.pop(name, None)


def _layered_project(tmp_path, monkeypatch, main_body):
    # The project root is on sys.path and every import is relative to it.
    root = tmp_path / "shop"
    for layer in LAYERS[:-1]:
        (root / layer).mkdir(parents=True)
        (root / layer / "__init__.py").write_text("")
    (root / "requirements.txt").write_text("")
    for rel, body in FILES.items():
        (root / rel).write_text(body)
    (root / "main.py").write_text(main_body)
    _forget_project_modules()
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    return root


@pytest.fixture
def plain_project(tmp_path, monkeypatch):
    yield _layered_project(tmp_path, monkeypatch, "from bootstrap.composition import container\n")
    _forget_project_modules()


def test_auto_inject_finds_the_implementation_in_another_layer(plain_project):
    composition = importlib.import_module("bootstrap.composition")
    autowired.auto_inject(composition.container, root_dir=str(plain_project))
    controller_mod = importlib.import_module("presentation.product_controller")
    with composition.container.create_scope():
        assert controller_mod.ProductController().list_products() == ["Notebook", "Pencil"]


# ---- FastAPI: no root_dir, no lifespan ----------------------------------------------
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def fastapi_project(tmp_path, monkeypatch):
    # The server's own module is __main__, as with `uvicorn main:app`.
    server_main = types.ModuleType("__main__")
    server_main.__file__ = str(tmp_path / "elsewhere" / "server" / "__main__.py")
    monkeypatch.setitem(sys.modules, "__main__", server_main)
    yield _layered_project(tmp_path, monkeypatch, FASTAPI_MAIN)
    _forget_project_modules()


def test_attach_container_needs_no_root_dir(fastapi_project):
    main = importlib.import_module("main")
    with TestClient(main.app) as client:
        assert client.get("/products").json() == ["Notebook", "Pencil"]


def test_shutdown_disposes_singletons_and_a_restart_gets_fresh_ones(fastapi_project):
    main = importlib.import_module("main")
    repo_mod = importlib.import_module("infrastructure.memory_product_repository")
    closed = repo_mod.MemoryProductRepository.closed
    with TestClient(main.app) as client:
        client.get("/products")
    assert len(closed) == 1
    with TestClient(main.app) as client:                 # e.g. a second test using the app
        assert client.get("/products").json() == ["Notebook", "Pencil"]
    assert len(closed) == 2 and closed[0] is not closed[1]


def test_supplied_instances_are_not_disposed_and_closing_can_be_turned_off(tmp_path):
    from py_autowired.fastapi import attach_container

    class Owned:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    owned = Owned()
    container = Container()
    container.register_instance(Owned, owned)
    app = fastapi.FastAPI()
    attach_container(app, container, root_dir=str(tmp_path))
    with TestClient(app):
        pass
    assert owned.closed is False

    class Created:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    keep = Container()
    keep.register_singleton(Created)
    app2 = fastapi.FastAPI()
    attach_container(app2, keep, root_dir=str(tmp_path), close_on_shutdown=False)
    with TestClient(app2):
        made = keep.resolve(Created)
    assert made.closed is False
    assert keep.resolve(Created) is made
