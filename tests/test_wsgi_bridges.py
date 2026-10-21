"""0.5.1: one-call bridges for Flask and Django, and discovery inside folders
whose names are excluded (``.../examples/my_app``)."""

import atexit
import importlib
import sys
import wsgiref.util

import pytest

from py_autowired import autowired

from test_simple_setup import FILES, LAYERS, _forget_project_modules


def _project(base, main_name, main_body, extra=None):
    root = base / "examples" / "shop"           # an excluded name ABOVE the project root
    for layer in LAYERS[:-1]:
        (root / layer).mkdir(parents=True)
        (root / layer / "__init__.py").write_text("")
    (root / "requirements.txt").write_text("")
    files = dict(FILES)
    files["bootstrap/composition.py"] = files["bootstrap/composition.py"].replace(
        "register_singleton(IProductRepository)", "register_scoped(IProductRepository)")
    files.update(extra or {})
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    (root / main_name).write_text(main_body)
    return root


@pytest.fixture
def project_env(monkeypatch):
    _forget_project_modules()
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    hooks = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: hooks.append(fn) or fn)
    yield hooks
    _forget_project_modules()
    for name in [n for n in sys.modules if n.split(".")[0] in ("app", "urls", "settings", "register")]:
        sys.modules.pop(name, None)


def _call(wsgi_app, path):
    environ = {}
    wsgiref.util.setup_testing_defaults(environ)
    environ["PATH_INFO"] = path
    status = []
    result = wsgi_app(environ, lambda s, h, exc_info=None: status.append(s))
    try:
        body = b"".join(result)
    finally:
        getattr(result, "close", lambda: None)()
    return status[0], body


# ---- discovery: excluded names are checked inside the project only -----------
def test_project_below_an_examples_folder_is_still_discovered(tmp_path, monkeypatch, project_env):
    root = _project(tmp_path, "main.py", "")
    monkeypatch.syspath_prepend(str(root))
    composition = importlib.import_module("bootstrap.composition")
    autowired.auto_inject(composition.container, root_dir=str(root))
    controller = importlib.import_module("presentation.product_controller")
    with composition.container.create_scope():
        assert controller.ProductController().list_products() == ["Notebook", "Pencil"]


# ---- Flask -------------------------------------------------------------------------
FLASK_APP = (
    "from flask import Flask\n"
    "from py_autowired.flask import attach_container\n"
    "from bootstrap.composition import container\n"
    "from presentation.product_controller import ProductController\n"
    "app = Flask(__name__)\n"
    "attach_container(app, container)\n"
    "@app.get('/products')\n"
    "def products():\n"
    "    return ProductController().list_products()\n")


def test_flask_bridge_one_call(tmp_path, monkeypatch, project_env):
    pytest.importorskip("flask")
    root = _project(tmp_path, "app.py", FLASK_APP)
    monkeypatch.syspath_prepend(str(root))
    app_mod = importlib.import_module("app")
    repo_mod = importlib.import_module("infrastructure.memory_product_repository")
    closed = repo_mod.MemoryProductRepository.closed
    closed.clear()
    status, body = _call(app_mod.app.wsgi_app, "/products")
    assert status.startswith("200") and b"Notebook" in body
    assert len(closed) == 1                         # request scope ended with the response
    _call(app_mod.app.wsgi_app, "/products")
    assert len(closed) == 2 and closed[0] is not closed[1]
    assert project_env == [app_mod.container._release_singletons_at_exit]


def test_flask_bridge_rejects_a_second_container(tmp_path, project_env):
    flask = pytest.importorskip("flask")
    from py_autowired import Container
    from py_autowired.flask import attach_container

    app = flask.Flask("x")
    attach_container(app, Container(), root_dir=str(tmp_path))
    with pytest.raises(ValueError, match="already attached"):
        attach_container(app, Container(), root_dir=str(tmp_path))


def test_flask_bridge_can_leave_singletons_alone(tmp_path, project_env):
    flask = pytest.importorskip("flask")
    from py_autowired import Container
    from py_autowired.flask import attach_container

    attach_container(flask.Flask("y"), Container(), root_dir=str(tmp_path), close_on_shutdown=False)
    assert project_env == []


# ---- Django ------------------------------------------------------------------------
DJANGO_FILES = {
    "settings.py": (
        "SECRET_KEY = 'test'\nDEBUG = True\nALLOWED_HOSTS = ['*']\nROOT_URLCONF = 'urls'\n"
        "INSTALLED_APPS = []\nMIDDLEWARE = []\nDATABASES = {}\n"),
    "urls.py": (
        "from django.http import JsonResponse\n"
        "from django.urls import path\n"
        "from presentation.product_controller import ProductController\n"
        "def products(request):\n"
        "    return JsonResponse(ProductController().list_products(), safe=False)\n"
        "urlpatterns = [path('products', products)]\n"),
    "register/__init__.py": "",
    "register/register.py": (
        "from py_autowired.django import attach_container\n"
        "from bootstrap.composition import container\n"
        "def register(application):\n"
        "    return attach_container(application, container)\n"),
}


def test_django_bridge_one_call(tmp_path, monkeypatch, project_env):
    django = pytest.importorskip("django")
    from django.conf import settings

    if settings.configured:
        pytest.skip("Django settings already configured in this process")
    root = _project(tmp_path, "manage.py", "# manage.py marks the project root\n", DJANGO_FILES)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.wsgi import get_wsgi_application

    register = importlib.import_module("register.register")
    application = register.register(get_wsgi_application())
    repo_mod = importlib.import_module("infrastructure.memory_product_repository")
    closed = repo_mod.MemoryProductRepository.closed
    closed.clear()
    status, body = _call(application, "/products")
    assert status.startswith("200") and body == b'["Notebook", "Pencil"]'
    assert len(closed) == 1
    assert project_env == [register.container._release_singletons_at_exit]


def test_django_bridge_wraps_an_asgi_application(tmp_path, project_env):
    import asyncio

    from py_autowired import Container
    from py_autowired.django import attach_container

    class Session:
        closed = []

        def close(self):
            Session.closed.append(self)

    container = Container()
    container.register_scoped(Session)
    seen = []

    class AsgiApp:                                  # stands in for Django's ASGIHandler
        async def __call__(self, scope, receive, send):
            seen.append((container.resolve(Session), container.resolve(Session)))

    wrapped = attach_container(AsgiApp(), container, root_dir=str(tmp_path))

    async def request():
        await wrapped({"type": "http"}, None, None)

    asyncio.run(request())
    asyncio.run(request())
    (a1, a2), (b1, b2) = seen
    assert a1 is a2 and b1 is b2 and a1 is not b1   # one Session per request
    assert Session.closed == [a1, b1]               # closed when each request ended
    assert project_env == [container._release_singletons_at_exit]
