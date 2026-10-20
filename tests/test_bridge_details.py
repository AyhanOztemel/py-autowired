"""0.5.1 bridge and shutdown details found by mutation testing."""

import abc
import asyncio
import importlib
import inspect
import sys
import uuid

import pytest

from py_autowired import Container, ResolutionError, autowired
from py_autowired import _web


# ---- public signatures ------------------------------------------------------------
@pytest.mark.parametrize("module", ["py_autowired.flask", "py_autowired.django", "py_autowired.fastapi"])
def test_bridge_options_are_keyword_only_with_safe_defaults(module):
    if module.endswith("fastapi"):
        pytest.importorskip("fastapi")
    params = inspect.signature(importlib.import_module(module).attach_container).parameters
    first, second, *options = params.values()
    assert first.kind is second.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert {p.name: p.kind for p in options} == {
        name: inspect.Parameter.KEYWORD_ONLY
        for name in ("root_dir", "exclude_dirs", "validate", "close_on_shutdown")}
    assert {p.name: p.default for p in options} == {
        "root_dir": None, "exclude_dirs": None, "validate": False, "close_on_shutdown": True}


# ---- shutdown release -------------------------------------------------------------
class Tracked:
    log = []

    def close(self):
        Tracked.log.append(self)


class Broken:
    def close(self):
        raise RuntimeError("broken close")


class Supplied:
    pass


def test_sync_release_closes_singletons_keeps_supplied_ones_and_recreates():
    Tracked.log.clear()
    supplied = Supplied()
    container = Container()
    container.register_singleton(Tracked)
    container.register_instance(Supplied, supplied)
    first = container.resolve(Tracked)
    container._release_singletons()
    assert Tracked.log == [first]
    assert container.resolve(Supplied) is supplied          # never reset
    second = container.resolve(Tracked)
    assert second is not first                              # fresh after a restart


@pytest.mark.parametrize("asynchronous", [False, True])
def test_release_reports_a_failing_close_after_closing_the_rest(asynchronous):
    Tracked.log.clear()
    container = Container()
    container.register_singleton(Tracked)
    container.register_singleton(Broken)
    tracked = container.resolve(Tracked)
    container.resolve(Broken)                               # closed first (reverse order)
    with pytest.raises(RuntimeError, match="broken close"):
        if asynchronous:
            asyncio.run(container._arelease_singletons())
        else:
            container._release_singletons()
    assert Tracked.log == [tracked]


def test_release_on_a_closed_container_does_nothing():
    container = Container()
    container.register_singleton(Tracked)
    container.resolve(Tracked)
    container.close()
    Tracked.log.clear()
    container._release_singletons()
    assert Tracked.log == []


# ---- deferred implementations at startup --------------------------------------------
def test_startup_completes_interfaces_so_implementation_names_match(tmp_path, monkeypatch):
    class IStore(abc.ABC):
        @abc.abstractmethod
        def get(self): ...

    container = Container()
    container.register_singleton(IStore)                    # implementation defined below

    class MemoryStore(IStore):
        def get(self):
            return "memory"

    class Missing(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container.register_transient(Missing)                   # never implemented: no startup error
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    autowired.auto_inject(container, root_dir=str(tmp_path))
    name_map = autowired._NAME_MAP_CACHE.get_raw_map()
    assert "memorystore" in name_map                         # field memory_store_instance matches
    assert container.registrations[IStore].implementation_type is MemoryStore
    with pytest.raises(ResolutionError):
        container.resolve(Missing)


# ---- shared WSGI / ASGI pieces ---------------------------------------------------------
def _startup_counting(monkeypatch):
    calls = []
    monkeypatch.setattr(_web, "auto_inject", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(_web, "activate_container", lambda c: None)
    return calls


def test_startup_runs_once(monkeypatch, tmp_path):
    calls = _startup_counting(monkeypatch)
    startup = _web.Startup(Container(), str(tmp_path), None, False)
    startup.ensure()
    startup.ensure()
    assert calls == [1]


class _Result:
    def __init__(self):
        self.closed = False

    def __iter__(self):
        return iter([b"body"])

    def close(self):
        self.closed = True


def test_wsgi_scope_closes_the_result_and_handles_results_without_close(monkeypatch, tmp_path):
    _startup_counting(monkeypatch)
    container = Container()
    startup = _web.Startup(container, str(tmp_path), None, False)
    result = _Result()
    wrapped = _web.WsgiRequestScope(lambda env, start: result, container, startup)
    out = wrapped({}, None)
    assert list(out) == [b"body"]
    out.close()
    assert result.closed is True
    plain = _web.WsgiRequestScope(lambda env, start: [b"x"], container, startup)({}, None)
    plain.close()                                           # a list has no close(): fine


def test_wsgi_scope_ends_when_the_application_raises(monkeypatch, tmp_path):
    _startup_counting(monkeypatch)
    Tracked.log.clear()
    container = Container()
    container.register_scoped(Tracked)
    startup = _web.Startup(container, str(tmp_path), None, False)

    def failing(environ, start_response):
        container.resolve(Tracked)
        raise LookupError("app failed")

    with pytest.raises(LookupError):
        _web.WsgiRequestScope(failing, container, startup)({}, None)
    assert len(Tracked.log) == 1
    assert container.current_scope is None


@pytest.mark.parametrize("kind", ["lifespan", "websocket"])
def test_asgi_scope_passes_other_event_types_through(monkeypatch, tmp_path, kind):
    calls = _startup_counting(monkeypatch)
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    container = Container()
    wrapped = _web.AsgiRequestScope(app, container, _web.Startup(container, str(tmp_path), None, False))
    asyncio.run(wrapped({"type": kind}, None, None))
    assert seen == [kind] and calls == []


def test_django_root_is_the_folder_with_manage_py(tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    site = tmp_path / "site"
    pkg = site / f"inner_{tag}"
    pkg.mkdir(parents=True)
    (site / "manage.py").write_text("")
    (pkg / "requirements.txt").write_text("")              # a nearer generic marker
    (pkg / f"where_{tag}.py").write_text(
        "from py_autowired import _web\n"
        "def django_root():\n    return _web.caller_root(0, marker='manage.py')\n"
        "def generic_root():\n    return _web.caller_root(0)\n")
    monkeypatch.syspath_prepend(str(pkg))
    module = importlib.import_module(f"where_{tag}")
    try:
        assert module.django_root() == str(site.resolve())
        assert module.generic_root() == str(pkg.resolve())
    finally:
        sys.modules.pop(f"where_{tag}", None)


# ---- FastAPI: shutdown finishes even while the lifespan is being cancelled -------------------
def test_fastapi_shutdown_release_is_shielded_from_cancellation(tmp_path):
    fastapi = pytest.importorskip("fastapi")
    anyio = pytest.importorskip("anyio")
    from py_autowired.fastapi import attach_container

    done = []

    class SlowClose:
        async def aclose(self):
            await anyio.sleep(0)
            done.append(True)

    container = Container()
    container.register_singleton(SlowClose)
    app = fastapi.FastAPI()
    attach_container(app, container, root_dir=str(tmp_path))

    async def main():
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        container.resolve(SlowClose)
        with anyio.CancelScope() as cancel:
            cancel.cancel()
            await lifespan.__aexit__(None, None, None)

    anyio.run(main)
    assert done == [True]
