"""0.5.4: findings of the independent blind review (N1-N15), each pinned by a test.

Threads in these tests are daemons and every join(3) is followed by an
"is it still alive" assertion: a hang fails the test instead of the run.
"""

import threading
import time

import pytest

from py_autowired import CircularDependencyError, Container, Lazy


def _run(target):
    """Run ``target`` in a daemon thread; return (finished, result, error)."""
    box = {}

    def work():
        try:
            box["result"] = target()
        except BaseException as error:  # noqa: BLE001 - the test inspects it
            box["error"] = error

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(3)
    return not thread.is_alive(), box.get("result"), box.get("error")


# ---- N4: Lazy.value read again while it is being resolved -----------------------------------

def test_lazy_read_again_during_its_own_resolution_is_an_error_not_a_hang():
    container = Container()
    holder = {}

    class Service:
        def __init__(self):
            holder["lazy"].value                       # the service needs its own Lazy

    container.register_transient(Service)
    holder["lazy"] = Lazy(container, Service)
    finished, _result, error = _run(lambda: holder["lazy"].value)
    assert finished, "Lazy.value hung on its own lock"
    assert isinstance(error, CircularDependencyError)
    assert "Service" in str(error)


def test_lazy_reentry_never_resolves_twice(monkeypatch):
    container = Container()
    holder, calls = {}, []

    class Service:
        def __init__(self):
            try:
                holder["lazy"].value
            except CircularDependencyError:
                pass                                   # even if the service swallows it ...

    container.register_transient(Service)
    real = container.resolve

    def counting(service_type):
        calls.append(service_type)
        return real(service_type)

    monkeypatch.setattr(container, "resolve", counting)
    holder["lazy"] = Lazy(container, Service)
    finished, result, _error = _run(lambda: holder["lazy"].value)
    assert finished
    assert calls == [Service]                         # ... it is resolved exactly once
    assert holder["lazy"].value is result


def test_lazy_read_by_two_threads_at_once_creates_one_value():
    container = Container()
    created = []

    class Slow:
        def __init__(self):
            created.append(self)
            time.sleep(0.2)

    container.register_transient(Slow)
    lazy = Lazy(container, Slow)
    start = threading.Barrier(2)
    results = []

    def read():
        start.wait(2)
        results.append(lazy.value)

    threads = [threading.Thread(target=read, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not any(thread.is_alive() for thread in threads)
    assert len(created) == 1 and results[0] is results[1]


# ---- N5: a scoped creation must not block other slots of its scope ---------------------------

import contextvars  # noqa: E402

from py_autowired import DependencyInjectionError, LifetimeScope  # noqa: E402


class _ScopedA:
    pass


def test_a_scoped_factory_can_wait_for_a_worker_that_needs_another_scoped_service():
    """Factory B starts a worker in the same scope; the worker needs scoped A (not created yet)."""
    container = Container()
    container.register_scoped(_ScopedA)

    def make_b(c):
        context = contextvars.copy_context()           # the worker runs in the same scope
        box = {}
        worker = threading.Thread(target=lambda: box.setdefault("a", context.run(c.resolve, _ScopedA)),
                                  daemon=True)
        worker.start()
        worker.join(2)                                 # a real factory might wait forever
        return ("B", box.get("a"))

    container.register_factory(tuple, make_b, LifetimeScope.SCOPED)
    with container.create_scope():
        context = contextvars.copy_context()
        finished, b, error = _run(lambda: context.run(container.resolve, tuple))
    assert finished and error is None
    assert isinstance(b[1], _ScopedA), "the worker waited for the whole scope while B was created"


def test_two_scopes_create_the_same_scoped_type_independently():
    container = Container()
    started = []

    class Slow:
        def __init__(self):
            started.append(time.monotonic())
            time.sleep(0.3)

    container.register_scoped(Slow)
    results = []

    def in_its_own_scope():
        with container.create_scope():
            results.append(container.resolve(Slow))

    threads = [threading.Thread(target=in_its_own_scope, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)
    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2 and results[0] is not results[1]
    assert abs(started[0] - started[1]) < 0.25                  # neither waited for the other


def test_closing_a_scope_from_inside_one_of_its_creations_is_an_error_not_a_hang():
    container = Container()
    holder = {}

    def make(c):
        holder["scope"].state.close()                 # closes the scope it is being created in
        return "made"

    container.register_factory(str, make, LifetimeScope.SCOPED)
    scope = container.create_scope()
    holder["scope"] = scope

    def attempt():
        with scope:
            return container.resolve(str)

    finished, _result, error = _run(attempt)
    assert finished, "close() inside a creation hung"
    assert isinstance(error, DependencyInjectionError)
    assert "str" in str(error)


def test_creation_gates_do_not_pile_up_in_a_long_lived_scope():
    container = Container()
    container.register_scoped(_ScopedA)
    with container.create_scope() as scope:
        container.resolve(_ScopedA)
        assert getattr(scope.state, "gates", {}) == {}


def test_control_a_creation_in_flight_is_finished_and_closed_with_the_scope():
    container = Container()
    disposed = []

    class Slow:
        def __init__(self):
            time.sleep(0.3)

        def dispose(self):
            disposed.append(self)

    container.register_scoped(Slow)
    scope = container.create_scope()
    scope.__enter__()
    context = contextvars.copy_context()
    creator = threading.Thread(target=lambda: context.run(container.resolve, Slow), daemon=True)
    creator.start()
    time.sleep(0.1)                                    # the creation is in flight now
    scope.__exit__(None, None, None)
    creator.join(3)
    assert not creator.is_alive()
    assert len(disposed) == 1


# ---- shared helpers for N1-N15 --------------------------------------------------------------

import abc  # noqa: E402
import asyncio  # noqa: E402
import gc  # noqa: E402
import importlib  # noqa: E402
import logging  # noqa: E402
import sys  # noqa: E402
import textwrap  # noqa: E402
import uuid  # noqa: E402

from py_autowired import (  # noqa: E402
    AsyncDisposalRequiredError,
    AutowiringValidationError,
    DuplicateServiceRegistrationError,
    Provider,
    autowired,
    validate_autowiring,
)


@pytest.fixture
def graph(monkeypatch):
    container = Container()
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)

    def patch(*classes):
        for cls in classes:
            autowired._patch_setattr(cls, container, cache.get_raw_map())
            autowired._patch_init(cls, container, cache.get_raw_map())

    return container, patch


def _call_here(target):
    """Call in this thread; return (True, result, error) - never raises."""
    try:
        return True, target(), None
    except BaseException as error:  # noqa: BLE001 - the test inspects it
        return True, None, error


def _issues(container):
    try:
        validate_autowiring(container)
    except AutowiringValidationError as error:
        return error.issues
    return ()


def _warnings(caplog, *parts):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and all(p in r.getMessage() for p in parts)]


class IRepoN1(abc.ABC):
    @abc.abstractmethod
    def get(self): ...


class RepoN1A(IRepoN1):
    def get(self):
        return "a"


class RepoN1B(IRepoN1):
    def get(self):
        return "b"


# ---- N1: a plural field declared in the class body receives every registration ------------

def test_n1_a_plural_field_in_the_class_body_is_a_list(graph):
    container, patch = graph
    container.register_singleton(IRepoN1, RepoN1A)
    container.register_singleton(IRepoN1, RepoN1B)

    class Holder:
        repon1_instances = None

    patch(Holder)
    value = Holder().repon1_instances
    assert isinstance(value, list) and [type(item) for item in value] == [RepoN1A, RepoN1B]


# ---- N2: a positional-only parameter is passed by position (0.5.4 A1; was a validation issue) ---

class _ServiceN2:
    pass


class _PositionalN2:
    def __init__(self, dependency: _ServiceN2, /):
        self.dependency = dependency


def test_n2_a_positional_only_dependency_is_injected(graph):
    container, patch = graph
    container.register_transient(_ServiceN2)
    container.register_transient(_PositionalN2)
    consumer = type("ConsumerN2", (), {"__module__": __name__, "positionaln2_instance": None})
    patch(consumer)
    assert isinstance(container.resolve(_PositionalN2).dependency, _ServiceN2)
    assert _issues(container) == ()


# ---- N3: a singleton holding Lazy[Scoped] keeps the first scope's object -------------------

class _SessionN3:
    pass


class _HolderN3:
    def __init__(self, session: Lazy[_SessionN3]):
        self.session = session


class _ProviderHolderN3:
    def __init__(self, session: Provider[_SessionN3]):
        self.session = session


def test_n3_a_singleton_with_lazy_scoped_is_a_capture(graph):
    container, patch = graph
    container.register_scoped(_SessionN3)
    container.register_singleton(_HolderN3)
    consumer = type("ConsumerN3", (), {"__module__": __name__, "holdern3_instance": None})
    patch(consumer)
    found = [issue for issue in _issues(container) if "captures scoped _SessionN3" in issue]
    assert found and "Lazy" in found[0]


def test_n3_control_a_provider_is_not_a_capture(graph):
    container, patch = graph
    container.register_scoped(_SessionN3)
    container.register_singleton(_ProviderHolderN3)
    consumer = type("ConsumerN3P", (), {"__module__": __name__, "providerholdern3_instance": None})
    patch(consumer)
    assert not [issue for issue in _issues(container) if "captures" in issue]


# ---- N6: a scoped service resolved while a singleton is created is a WARNING ---------------

class _RequestN6:
    pass


class _MiddleN6:
    def __init__(self, request: _RequestN6):
        self.request = request


class _CacheN6:
    def __init__(self, middle: _MiddleN6):
        self.middle = middle


def test_n6_a_singleton_resolving_a_scoped_service_is_a_warning_once(caplog):
    container = Container()
    container.register_scoped(_RequestN6)
    container.register_transient(_MiddleN6)                              # through a transient
    container.register_singleton(_CacheN6)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        with container.create_scope():
            cache = container.resolve(_CacheN6)
            container.resolve(_CacheN6)
    assert isinstance(cache.middle.request, _RequestN6)                  # behaviour unchanged
    found = _warnings(caplog, "_RequestN6", "_CacheN6")
    assert len(found) == 1 and "while singleton" in found[0]


def test_n6_the_warning_names_the_innermost_singleton(caplog):
    container = Container()

    class Inner:
        def __init__(self, request: _RequestN6):
            self.request = request

    class Outer:
        def __init__(self, inner: Inner):
            self.inner = inner

    container.register_scoped(_RequestN6)
    container.register_singleton(Inner)
    container.register_singleton(Outer)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        with container.create_scope():
            container.resolve(Outer)
    found = _warnings(caplog, "_RequestN6", "while singleton")
    assert len(found) == 1 and "Inner" in found[0] and "Outer" not in found[0]


def test_n6_no_warning_is_written_while_a_creation_lock_is_held(caplog):
    """A log handler may itself take locks: library warnings wait until no creation lock is held."""
    container = Container()

    class RequestProbe:
        pass

    class CacheProbe:
        def __init__(self, request: RequestProbe):
            self.request = request

    container.register_scoped(RequestProbe)
    container.register_singleton(CacheProbe)
    seen = []

    class Probe(logging.Handler):
        def emit(self, record):
            seen.append(any(owner for owner in __import__("py_autowired.container", fromlist=["x"])
                            ._LOCK_OWNERS.values()))

    probe = Probe()
    logging.getLogger("py_autowired").addHandler(probe)
    try:
        with container.create_scope():
            container.resolve(CacheProbe)
    finally:
        logging.getLogger("py_autowired").removeHandler(probe)
    assert seen and not any(seen)


# ---- N7: a keyed singleton's fields are checked for scoped capture ------------------------

def test_n7_a_keyed_singleton_capturing_a_scoped_field_is_reported(graph):
    container, patch = graph

    class SessionN7:
        pass

    class KeyedOwner:
        sessionn7_instance = None

    container.register_scoped(SessionN7)
    container.register_keyed(KeyedOwner, "main", KeyedOwner, LifetimeScope.SINGLETON)
    patch(KeyedOwner)
    assert any("singleton KeyedOwner captures scoped SessionN7" in issue for issue in _issues(container))


# ---- N9 + N10: a module that cannot be imported or patched is a validation issue ----------

@pytest.fixture
def broken_project(tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"brk_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / f"good_{tag}.py").write_text("X = 1\n")
    (root / f"broken_{tag}.py").write_text("import does_not_exist_pkg_n10\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    before = set(sys.modules)
    yield root, tag
    for name in set(sys.modules) - before:
        if tag in name:
            sys.modules.pop(name, None)


def test_n10_a_module_discovery_could_not_import_fails_validation(broken_project):
    root, tag = broken_project
    container = Container()
    _finished, _result, error = _call_here(lambda: autowired.auto_inject(container, root_dir=str(root), validate=True))
    assert isinstance(error, AutowiringValidationError)
    assert any(issue.startswith(f"broken_{tag}: could not be imported during discovery")
               and "does_not_exist_pkg_n10" in issue for issue in error.issues)


def test_n10_without_validate_it_is_reported_by_a_later_validation(broken_project):
    root, tag = broken_project
    container = Container()
    autowired.auto_inject(container, root_dir=str(root))
    assert any(f"broken_{tag}: could not be imported" in issue for issue in _issues(container))


def test_n9_a_late_module_that_cannot_be_patched_is_an_error_and_an_issue(broken_project, monkeypatch, caplog):
    root, tag = broken_project
    (root / "dist").mkdir()
    (root / "dist" / f"late_{tag}.py").write_text("class Late:\n    clock_instance = None\n")
    monkeypatch.syspath_prepend(str(root / "dist"))
    container = Container()
    (root / f"broken_{tag}.py").write_text("X = 2\n")                     # only the late module fails
    autowired.auto_inject(container, root_dir=str(root))

    def broken(*_args):
        raise RuntimeError("cannot patch late")

    monkeypatch.setattr(autowired, "_patch_module_classes", broken)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"late_{tag}")
    assert [r.levelname for r in caplog.records if "late-imported module" in r.getMessage()] == ["ERROR"]
    monkeypatch.undo()
    assert any(f"late_{tag}: could not be patched" in issue for issue in _issues(container))


# ---- N11: strict registrations also guard keyed and open-generic registrations -------------

def test_n11_strict_keyed_registration_cannot_be_replaced_silently():
    container = Container(strict_registrations=True)
    container.register_keyed(_ServiceN2, "k", _ServiceN2)
    _f, _r, error = _call_here(lambda: container.register_keyed(_ServiceN2, "k", _ServiceN2))
    assert isinstance(error, DuplicateServiceRegistrationError)


def test_n11_strict_open_generic_registration_cannot_be_replaced_silently():
    from typing import Generic, TypeVar
    T = TypeVar("T")

    class RepoG(Generic[T]):
        pass

    container = Container(strict_registrations=True)
    container.register_open_generic(RepoG)
    _f, _r, error = _call_here(lambda: container.register_open_generic(RepoG))
    assert isinstance(error, DuplicateServiceRegistrationError)


def test_n11_control_without_strict_they_are_replaced_as_before():
    container = Container()
    container.register_keyed(_ServiceN2, "k", _ServiceN2)
    container.register_keyed(_ServiceN2, "k", _ServiceN2)


# ---- N12: bridges close async-only singletons at process exit -------------------------------

def test_n12_the_exit_hook_closes_an_async_only_singleton(monkeypatch):
    from py_autowired import django as django_bridge
    registered = []
    monkeypatch.setattr(django_bridge.atexit, "register", lambda function, *a: registered.append((function, a)))
    closed = []

    class AsyncOnly:
        async def aclose(self):
            closed.append(self)

    container = Container()
    container.register_singleton(AsyncOnly)

    async def application(scope, receive, send):
        pass

    django_bridge.attach_container(application, container, root_dir=".")
    container.resolve(AsyncOnly)
    [(function, args)] = registered
    _f, _r, error = _call_here(lambda: function(*args))
    assert error is None and len(closed) == 1


# ---- N13: an ASGI request scope finishes its cleanup even when the request is cancelled ----

def test_n13_asgi_cleanup_survives_cancellation():
    from py_autowired._web import AsgiRequestScope
    finished = []

    class Session:
        async def aclose(self):
            await asyncio.sleep(0.1)
            finished.append(True)

    container = Container()
    container.register_scoped(Session)

    async def application(scope, receive, send):
        container.resolve(Session)

    class _Ready:
        def ensure(self):
            pass

    wrapped = AsgiRequestScope(application, container, _Ready())

    async def main():
        task = asyncio.ensure_future(wrapped({"type": "http"}, None, None))
        await asyncio.sleep(0.03)                 # the request ended; cleanup is awaiting aclose
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert finished == [True]


# ---- N14: a failing log handler does not lose the other disposal errors --------------------

def test_n14_a_broken_log_handler_does_not_lose_disposal_notes():
    from py_autowired import container as container_module

    class Exploding(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler broke")

        def handleError(self, record):
            raise RuntimeError("handler broke")

    handler = Exploding()
    logger = logging.getLogger("py_autowired")
    logger.addHandler(handler)
    try:
        first, second = ValueError("first"), KeyError("second")
        with pytest.raises(ValueError) as caught:
            container_module._raise_disposal_errors([first, second])
    finally:
        logger.removeHandler(handler)
    assert any("KeyError" in note for note in getattr(caught.value, "__notes__", ()))


# ---- N15: a non-callable attribute does not hide the real finalizer -----------------------

@pytest.mark.parametrize("kind", ["sync", "async"])
def test_n15_the_first_callable_finalizer_is_used(kind):
    closed = []

    class Resource:
        dispose = True                              # a flag, not a method
        aclose = None

        def close(self):
            closed.append(self)

    container = Container()
    container.register_scoped(Resource)
    if kind == "sync":
        with container.create_scope():
            container.resolve(Resource)
    else:
        async def main():
            async with container.create_scope():
                container.resolve(Resource)
        asyncio.run(main())
    assert len(closed) == 1


# ---- N5 limit made visible: a long wait at a creation gate is a WARNING -------------------

@pytest.mark.parametrize("lifetime", [LifetimeScope.SINGLETON, LifetimeScope.SCOPED])
def test_a_long_wait_for_a_creation_is_a_warning(caplog, lifetime):
    """The factory joins a worker that needs the very service being created: no lock shows
    that wait, so it cannot raise - but after the threshold it is reported, once."""
    container = Container()
    container.creation_wait_warning = 0.2

    class Joined:
        pass

    def make(c):
        context = contextvars.copy_context()

        def work():
            try:
                context.run(c.resolve, Joined)
            except Exception:  # noqa: BLE001 - the scope may be closed when it gets in
                pass

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(1.0)                               # a real factory might wait forever
        return Joined()

    container.register_factory(Joined, make, lifetime)

    def resolve():
        if lifetime is LifetimeScope.SCOPED:
            with container.create_scope():
                return container.resolve(Joined)
        return container.resolve(Joined)

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        finished, _result, error = _run(resolve)
        time.sleep(0.2)
    assert finished and error is None
    found = [r.getMessage() for r in caplog.records
             if r.levelno == logging.WARNING and "has waited" in r.getMessage()]
    assert len(found) == 1 and "Joined" in found[0] and "deadlock" in found[0]


# ---- branch guards for the N fixes (each keeps a mutant from surviving) ----------------------

def test_n4_a_failed_resolution_can_be_retried_in_the_same_thread():
    container = Container()
    attempts = []

    class Flaky:
        def __init__(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("first time fails")

    container.register_transient(Flaky)
    lazy = Lazy(container, Flaky)
    with pytest.raises(RuntimeError):
        lazy.value
    assert isinstance(lazy.value, Flaky)                    # not a false "read again" error


def test_n5_an_inner_cycle_does_not_open_the_gate_for_a_second_object():
    """The factory catches the cycle error of a nested request for its own slot and goes on;
    another thread asking meanwhile must still wait for the one object."""
    container = Container()
    made, other = [], {}
    started = threading.Event()

    def make(c):
        try:
            c.resolve(dict)                                 # the same slot: a cycle
        except CircularDependencyError:
            pass
        started.set()
        time.sleep(0.3)
        made.append(1)
        return {"n": len(made)}

    container.register_factory(dict, make, LifetimeScope.SCOPED)
    with container.create_scope():
        context = contextvars.copy_context()
        racer = threading.Thread(
            target=lambda: (started.wait(2), other.setdefault("v", context.run(container.resolve, dict))),
            daemon=True)
        racer.start()
        first = container.resolve(dict)
        racer.join(3)
        assert not racer.is_alive()
    assert other["v"] is first and len(made) == 1


def test_n5_a_thread_that_gets_the_gate_after_close_does_not_create():
    container = Container()
    calls = []
    entered = threading.Event()

    def make(c):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            time.sleep(0.3)
            raise RuntimeError("first creation fails")
        return "second"

    container.register_factory(str, make, LifetimeScope.SCOPED)
    scope = container.create_scope()
    scope.__enter__()
    box = {}

    def ask(key, context):
        try:
            box[key] = context.run(container.resolve, str)
        except Exception as error:  # noqa: BLE001
            box[key] = error

    first = threading.Thread(target=ask, args=("first", contextvars.copy_context()), daemon=True)
    first.start()
    entered.wait(2)
    second = threading.Thread(target=ask, args=("second", contextvars.copy_context()), daemon=True)
    second.start()                                          # waits at the gate
    time.sleep(0.05)
    scope.__exit__(None, None, None)                        # closes while both are inside
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    from py_autowired import ScopeNotActiveError
    assert isinstance(box["second"], ScopeNotActiveError)
    assert calls == [1]                                     # nothing created in the closed scope


class _PlainN6:
    pass


class _ScopedN6b:
    pass


def test_n6_the_owner_is_cleared_after_the_singleton_is_created(caplog):
    container = Container()
    container.register_singleton(_PlainN6)
    container.register_scoped(_ScopedN6b)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        with container.create_scope():
            container.resolve(_PlainN6)
            container.resolve(_ScopedN6b)                   # not inside a singleton creation
    assert _warnings(caplog, "_ScopedN6b") == []


def test_n6_several_deferred_warnings_are_all_written(caplog):
    container = Container()

    class FirstScoped:
        pass

    class SecondScoped:
        pass

    class Both:
        def __init__(self, first: FirstScoped, second: SecondScoped):
            self.first, self.second = first, second

    container.register_scoped(FirstScoped)
    container.register_scoped(SecondScoped)
    container.register_singleton(Both)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        with container.create_scope():
            container.resolve(Both)
    assert len(_warnings(caplog, "FirstScoped", "while singleton")) == 1
    assert len(_warnings(caplog, "SecondScoped", "while singleton")) == 1


def test_n13_the_request_leaves_its_scope():
    from py_autowired._web import AsgiRequestScope
    container = Container()

    async def application(scope, receive, send):
        assert container.current_scope is not None

    class _Ready:
        def ensure(self):
            pass

    wrapped = AsgiRequestScope(application, container, _Ready())

    async def main():
        await wrapped({"type": "http"}, None, None)
        return container.current_scope

    assert asyncio.run(main()) is None


def test_n14_three_disposal_errors_keep_both_notes_even_with_a_broken_handler():
    from py_autowired import container as container_module

    class Exploding(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler broke")

    handler = Exploding()
    logger = logging.getLogger("py_autowired")
    logger.addHandler(handler)
    try:
        with pytest.raises(ValueError) as caught:
            container_module._raise_disposal_errors([ValueError("first"), KeyError("second"), OSError("third")])
    finally:
        logger.removeHandler(handler)
    notes = getattr(caught.value, "__notes__", ())
    assert any("KeyError" in note for note in notes) and any("OSError" in note for note in notes)


def test_long_wait_control_a_short_wait_is_not_reported(caplog):
    container = Container()
    container.creation_wait_warning = 5.0
    gate = threading.Event()

    class Slow:
        def __init__(self):
            gate.set()
            time.sleep(0.3)

    container.register_singleton(Slow)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        first = threading.Thread(target=lambda: container.resolve(Slow), daemon=True)
        first.start()
        gate.wait(2)
        container.resolve(Slow)                             # waits ~0.3 s, far below 5 s
        first.join(3)
    assert not first.is_alive()
    assert not [r for r in caplog.records if "has waited" in r.getMessage()]


def test_long_wait_while_holding_a_creation_lock_is_written_after_release(caplog):
    container = Container()
    container.creation_wait_warning = 0.1
    entered = threading.Event()

    class Held:
        def __init__(self):
            entered.set()
            time.sleep(0.4)

    class Outer:
        def __init__(self, held: Held):
            self.held = held

    container.register_singleton(Held)
    container.register_singleton(Outer)
    seen = []

    class Probe(logging.Handler):
        def emit(self, record):
            if "has waited" in record.getMessage():
                from py_autowired import container as container_module
                seen.append(any(owner[0] == threading.get_ident()
                                for owner in container_module._LOCK_OWNERS.values()))

    probe = Probe()
    logging.getLogger("py_autowired").addHandler(probe)
    try:
        holder = threading.Thread(target=lambda: container.resolve(Held), daemon=True)
        holder.start()
        entered.wait(2)
        container.resolve(Outer)                            # holds Outer's lock while waiting for Held
        holder.join(3)
    finally:
        logging.getLogger("py_autowired").removeHandler(probe)
    assert not holder.is_alive()
    assert seen == [False]                                  # written once, after its locks were released


class _KeyN2:
    pass


class _ProvidedN2:
    def __init__(self, dependency: Provider[_ServiceN2], /):
        self.dependency = dependency


def test_n2_positional_only_with_provider_or_keyed_is_injected(graph):
    from typing import Annotated
    from py_autowired import Keyed
    container, patch = graph

    class KeyedPositional:
        def __init__(self, dependency: Annotated[_KeyN2, Keyed("k")], /):
            self.dependency = dependency

    container.register_transient(_ServiceN2)
    container.register_keyed(_KeyN2, "k", _KeyN2)
    container.register_transient(_ProvidedN2)
    container.register_transient(KeyedPositional)
    consumer = type("ConsumerN2b", (), {"__module__": __name__, "keyedpositional_instance": None,
                                        "providedn2_instance": None})
    patch(consumer)
    assert _issues(container) == ()
    assert isinstance(container.resolve(_ProvidedN2).dependency(), _ServiceN2)
    assert isinstance(container.resolve(KeyedPositional).dependency, _KeyN2)


class _TransientLazyN3:
    def __init__(self, session: Lazy[_SessionN3]):
        self.session = session


class _SingleN3:
    pass


class _SingletonLazySingletonN3:
    def __init__(self, other: Lazy[_SingleN3]):
        self.other = other


def test_n3_control_lazy_is_a_capture_only_for_a_singleton_holding_a_scoped_service(graph):
    container, patch = graph
    container.register_scoped(_SessionN3)
    container.register_singleton(_SingleN3)
    container.register_transient(_TransientLazyN3)
    container.register_singleton(_SingletonLazySingletonN3)
    consumer = type("ConsumerN3c", (), {"__module__": __name__, "transientlazyn3_instance": None,
                                        "singletonlazysingletonn3_instance": None})
    consumer = type("ConsumerN3c", (), {"__module__": __name__, "_transientlazyn3_instance": None})
    consumer = type("ConsumerN3c", (), {"__module__": __name__, "transientlazyn3_instance": None,
                                        "singletonlazysingletonn3_instance": None})
    patch(consumer)
    assert not [issue for issue in _issues(container) if "captures" in issue]


def test_n10_a_module_shadowed_by_another_of_that_name_is_an_issue(tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    elsewhere, root = tmp_path / "elsewhere", tmp_path / f"shd_{tag}"
    elsewhere.mkdir()
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (elsewhere / f"shadow_{tag}.py").write_text("X = 'elsewhere'\n")
    (root / f"shadow_{tag}.py").write_text("X = 'project'\n")
    monkeypatch.syspath_prepend(str(elsewhere))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    importlib.import_module(f"shadow_{tag}")               # the other one is imported first
    try:
        container = Container()
        autowired.auto_inject(container, root_dir=str(root))
        autowired.auto_inject(container, root_dir=str(root))   # twice: still one issue
        found = [issue for issue in _issues(container) if f"shadow_{tag}: not loaded during discovery" in issue]
        assert len(found) == 1
    finally:
        sys.modules.pop(f"shadow_{tag}", None)


def test_n8_the_getter_warning_says_the_field_keeps_its_value(graph, caplog):
    container, patch = graph

    class Getter:
        @property
        def clock_instance(self):
            raise RuntimeError("getter fails")

        @clock_instance.setter
        def clock_instance(self, value):
            pass

    patch(Getter)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        Getter()
    assert any("keeps its current value" in message for message in _warnings(caplog, "Field read failed"))


# ---- second look in a real application (F1, F2): no false ambiguity, no property taken for a field

class IUnitF1(abc.ABC):
    @abc.abstractmethod
    def commit(self): ...


class ContextF1(IUnitF1):
    def commit(self):
        return "ok"


def test_f1_two_registrations_of_the_same_supplied_object_are_not_ambiguous(graph, caplog):
    container, patch = graph
    shared = ContextF1()
    container.register_instance(ContextF1, shared)
    container.register_instance(IUnitF1, shared)            # the same object under its contract
    consumer = type("ConsumerF1", (), {"__module__": __name__, "contextf1_instance": None})
    patch(consumer)
    assert not [issue for issue in _issues(container) if "ambiguous" in issue]
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired._warn_equal_candidates(container, autowired._build_name_map_from_container(container))
    assert _warnings(caplog, "equally good candidates") == []
    assert consumer().contextf1_instance is shared


def test_f1_control_two_separate_singletons_of_one_class_stay_ambiguous(graph):
    container, patch = graph

    class IReaderF1(abc.ABC):
        @abc.abstractmethod
        def read(self): ...

    class ReaderF1(IReaderF1):
        def read(self):
            return 1

    container.register_singleton(ReaderF1)
    container.register_singleton(IReaderF1, ReaderF1)        # a second, separate singleton
    consumer = type("ConsumerF1b", (), {"__module__": __name__, "readerf1_instance": None})
    patch(consumer)
    assert [issue for issue in _issues(container) if "ambiguous" in issue]


def test_f2_a_property_named_like_a_field_is_not_an_injection_field(graph):
    container, patch = graph

    class WithProperty:
        def __init__(self):
            self.value = None

        @property
        def has_instance(self):
            return self.value is not None

        def make_instance(self):
            return object()

    patch(WithProperty)
    assert "has_instance" not in WithProperty.__dict__.get("__di_attrs__", ())
    assert "make_instance" not in WithProperty.__dict__.get("__di_attrs__", ())
    assert not [issue for issue in _issues(container) if "WithProperty" in issue]


def test_f2_control_a_plain_field_is_still_a_field(graph):
    container, patch = graph

    class Plain:
        clock_instance = None

        def __init__(self):
            self.repo_instance = None

    patch(Plain)
    assert set(Plain.__dict__["__di_attrs__"]) == {"clock_instance", "repo_instance"}


def test_n11_control_without_strict_an_open_generic_is_replaced_as_before():
    from typing import Generic, TypeVar
    T = TypeVar("T")

    class RepoC(Generic[T]):
        pass

    class SqlRepoC(RepoC[T]):
        pass

    container = Container()
    container.register_open_generic(RepoC)
    _f, _r, error = _call_here(lambda: container.register_open_generic(RepoC, SqlRepoC))
    assert error is None
    assert type(container.resolve(RepoC[int])) is SqlRepoC
