"""0.5.4: one simple check at every request entry (FastAPI middleware, the WSGI/ASGI bridges) and
at the start of injected(): if another container is the active one, fail before anything is
built. No lock, no state, no counter. A best-effort catch, not a guarantee - one container per
process is supported (several applications in one process: 0.6.0). With no active container at
all (startup has not run) the behaviour is 0.5.3's."""

import asyncio
import logging

import pytest

from py_autowired import Container, DependencyInjectionError, autowired

fastapi = pytest.importorskip("fastapi")
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from py_autowired.fastapi import attach_container, injected  # noqa: E402


class _Clock:
    def __init__(self, owner):
        self.owner = owner


class _Built:
    count = 0


class _Controller:
    def __init__(self):
        _Built.count += 1
        self.clock_instance = None


def _app(name, tmp_path):
    container = Container()
    container.register_instance(_Clock, _Clock(name))
    app = FastAPI()

    @app.get("/owner")
    def owner(controller=Depends(injected(_Controller))):
        return {"clock": getattr(controller.clock_instance, "owner", None)}

    root = tmp_path / name
    root.mkdir()
    attach_container(app, container, root_dir=str(root))
    return app, container


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    _Built.count = 0


def _call(function, *args):
    try:
        return function(*args), None
    except Exception as error:  # noqa: BLE001 - the test inspects it
        return None, error


def _request_for(container):
    state = type("State", (), {"py_autowired_container": container})()
    app = type("App", (), {"state": state})()
    return type("Request", (), {"app": app})()


def test_sequential_web_applications_keep_working(tmp_path):
    app_a, _ = _app("a", tmp_path)
    app_b, _ = _app("b", tmp_path)
    for app, name in ((app_a, "a"), (app_b, "b"), (app_a, "a")):
        with TestClient(app) as client:
            assert client.get("/owner").json() == {"clock": name}


def test_a_request_while_another_container_is_active_fails_before_anything_is_built(tmp_path):
    app_a, _ = _app("a", tmp_path)
    other = Container()
    other.register_instance(_Clock, _Clock("other"))
    with TestClient(app_a, raise_server_exceptions=True) as client:
        autowired.activate_container(other)              # another container became active meanwhile
        response, error = _call(client.get, "/owner")
    assert isinstance(error, DependencyInjectionError), f"served from another container: {response!r}"
    assert _Built.count == 0                             # nothing was built for that request


def test_the_error_names_the_fix(tmp_path):
    app_a, _ = _app("a", tmp_path)
    with TestClient(app_a, raise_server_exceptions=True) as client:
        autowired.activate_container(Container())
        _response, error = _call(client.get, "/owner")
    assert "with TestClient(app)" in str(error) and "activate_container(container)" in str(error)


def test_two_applications_running_at_once_are_caught_at_the_first_request(tmp_path):
    app_a, _ = _app("a", tmp_path)
    app_b, _ = _app("b", tmp_path)
    with TestClient(app_a, raise_server_exceptions=True) as client_a:
        with TestClient(app_b):                          # its startup made its container the active one
            _response, error = _call(client_a.get, "/owner")
    assert isinstance(error, DependencyInjectionError), "a request was served from another container"
    assert _Built.count == 0


def test_injected_checks_its_container_before_building_anything():
    own, other = Container(), Container()
    own.register_instance(_Clock, _Clock("own"))
    other.register_instance(_Clock, _Clock("other"))
    own.register_transient(_Controller)                   # a registered type, resolved by the container
    autowired.activate_container(other)
    _made, error = _call(lambda: asyncio.run(injected(_Controller)(_request_for(own))))
    assert isinstance(error, DependencyInjectionError), f"built from another container: {_made!r}"
    _made, error = _call(lambda: asyncio.run(injected(_Clock)(_request_for(own))))  # unregistered path
    assert isinstance(error, DependencyInjectionError), f"built from another container: {_made!r}"
    assert _Built.count == 0


def test_without_any_active_container_injected_behaves_as_0_5_3(caplog):
    """Startup has not run (e.g. a TestClient used without `with`): nothing is activated, a field
    that is not found stays None, and the WARNING names the field and says why."""
    own = Container()
    own.register_instance(_Clock, _Clock("own"))

    class LonelyController:
        def __init__(self):
            self.clock_instance = None

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        made, error = _call(lambda: asyncio.run(injected(LonelyController)(_request_for(own))))
    assert error is None and made.clock_instance is None
    assert autowired._NAME_MAP_CACHE is None, "injected() made a container active"
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("LonelyController.clock_instance" in m and "startup has not run" in m for m in lines), lines


def test_without_startup_applications_used_one_after_another_do_not_fail(tmp_path):
    app_a, _ = _app("a", tmp_path)
    app_b, _ = _app("b", tmp_path)
    for app in (app_a, app_b, app_a):                     # no `with`: no startup, nothing active
        response, error = _call(TestClient(app, raise_server_exceptions=True).get, "/owner")
        assert error is None, f"a lifespan-less application failed: {error!r}"
        assert response.json() == {"clock": None}         # as in 0.5.3: the field stays None


def _app_with_plain_route(name, tmp_path):
    app, container = _app(name, tmp_path)

    @app.get("/plain")
    def plain():
        _Controller()                                   # no injected(): only the middleware can stop it
        return {"ok": True}

    return app, container


def test_the_middleware_alone_stops_a_request_of_a_non_active_container(tmp_path):
    app_a, _ = _app_with_plain_route("a", tmp_path)
    other = Container()
    with TestClient(app_a, raise_server_exceptions=True) as client:
        autowired.activate_container(other)
        _response, error = _call(client.get, "/plain")
    assert isinstance(error, DependencyInjectionError)
    assert _Built.count == 0


class _NoStartup:
    def ensure(self):
        pass


def test_the_wsgi_and_asgi_bridges_check_before_building():
    from py_autowired import _web

    own, other = Container(), Container()
    autowired.activate_container(other)

    def wsgi_app(environ, start_response):
        _Controller()
        return [b"x"]

    async def asgi_app(scope, receive, send):
        _Controller()

    _out, error = _call(lambda: _web.WsgiRequestScope(wsgi_app, own, _NoStartup())({}, None))
    assert isinstance(error, DependencyInjectionError)
    _out, error = _call(lambda: asyncio.run(_web.AsgiRequestScope(asgi_app, own, _NoStartup())({"type": "http"}, None, None)))
    assert isinstance(error, DependencyInjectionError)
    assert _Built.count == 0


def test_stopping_an_application_leaves_the_active_container_as_it_is(tmp_path):
    app_a, container_a = _app("a", tmp_path)
    with TestClient(app_a):
        pass
    assert autowired._NAME_MAP_CACHE._container is container_a
    other = Container()
    with TestClient(app_a):
        autowired.activate_container(other)               # e.g. a test switched containers meanwhile
    assert autowired._NAME_MAP_CACHE._container is other


def test_a_class_without_fields_is_built_without_any_line(caplog):
    own = Container()

    class NoFieldsController:
        pass

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        asyncio.run(injected(NoFieldsController)(_request_for(own)))
    assert not [r for r in caplog.records if "NoFieldsController" in r.getMessage()]


def test_two_applications_sharing_one_container_keep_working(tmp_path):
    shared = Container()
    shared.register_instance(_Clock, _Clock("shared"))
    first, second = FastAPI(), FastAPI()

    @first.get("/owner")
    def owner(controller=Depends(injected(_Controller))):
        return {"clock": getattr(controller.clock_instance, "owner", None)}

    for app, name in ((first, "first"), (second, "second")):
        root = tmp_path / name
        root.mkdir()
        attach_container(app, shared, root_dir=str(root), close_on_shutdown=False)
    with TestClient(first) as client:
        with TestClient(second):
            pass                                            # the second one stops; the first still runs
        assert client.get("/owner").json() == {"clock": "shared"}


def test_a_mounted_sub_application_with_the_same_container_serves_requests(tmp_path):
    parent, container = _app("parent", tmp_path)
    sub = FastAPI()

    @sub.get("/owner")
    def sub_owner(controller=Depends(injected(_Controller))):
        return {"clock": getattr(controller.clock_instance, "owner", None)}

    root = tmp_path / "sub"
    root.mkdir()
    attach_container(sub, container, root_dir=str(root))
    parent.mount("/sub", sub)
    with TestClient(parent) as client:
        response, error = _call(client.get, "/sub/owner")
    assert error is None, f"a mounted sub-application of the same container failed: {error!r}"
    assert response.json() == {"clock": "parent"}


def test_a_stopped_application_never_makes_patched_classes_fall_back_to_an_older_container(tmp_path):
    import importlib
    import sys
    import uuid

    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"proj_{tag}"
    root.mkdir()
    (root / f"consumer_{tag}.py").write_text("class Consumer:\n    def __init__(self):\n        self.clock_instance = None\n")
    sys.path.insert(0, str(root))
    try:
        apps = []
        for name in ("a", "b"):
            container = Container()
            container.register_instance(_Clock, _Clock(name))
            app = FastAPI()
            attach_container(app, container, root_dir=str(root), close_on_shutdown=False)
            apps.append(app)
        for app in apps:                                    # A runs and stops, then B runs and stops
            with TestClient(app):
                pass
        consumer = importlib.import_module(f"consumer_{tag}").Consumer()
        assert consumer.clock_instance.owner == "b", "a patched class fell back to an older container"
    finally:
        sys.path.remove(str(root))
        sys.modules.pop(f"consumer_{tag}", None)


def test_the_check_takes_no_lock(monkeypatch):
    own = Container()
    autowired.activate_container(own)
    taken = []

    class CountingLock:
        def __enter__(self):
            taken.append(1)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(autowired, "_GLOBAL_LOCK", CountingLock())
    for _ in range(3):
        autowired._check_active(own)
    assert taken == [], "the request check took the library lock"
