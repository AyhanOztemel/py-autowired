"""0.5.4, second blind review: each finding pinned by a test (threads are daemons; every
join(3) is followed by an "is it still alive" check, so a hang fails instead of freezing the run)."""

import abc
import asyncio
import contextvars
import gc
import importlib
import logging
import sys
import textwrap
import threading
import time
import uuid
from typing import Generic, TypeVar

import pytest

import py_autowired
from py_autowired import (
    AutowiringValidationError,
    CircularDependencyError,
    Container,
    Lazy,
    LifetimeScope,
    Provider,
    autowired,
    validate_autowiring,
)
from py_autowired import container as container_module

T = TypeVar("T")


def _run(target):
    box = {}

    def work():
        try:
            box["result"] = target()
        except BaseException as error:  # noqa: BLE001 - inspected by the test
            box["error"] = error

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, box


def _warnings(caplog, *parts):
    return [r.getMessage() for r in list(caplog.records)
            if r.levelno == logging.WARNING and all(p in r.getMessage() for p in parts)]


def _wait_for(predicate, seconds=3.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


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


def _issues(container):
    try:
        validate_autowiring(container)
    except AutowiringValidationError as error:
        return error.issues
    return ()


# ---- Y1 = Z1: two Lazy values read across two threads ----------------------------------

def test_y1_two_lazies_read_across_threads_raise_instead_of_hanging():
    container = Container()
    both = threading.Barrier(2)
    holder = {}

    class A:
        def __init__(self):
            both.wait(2)
            holder["b"].value

    class B:
        def __init__(self):
            both.wait(2)
            holder["a"].value

    container.register_transient(A)
    container.register_transient(B)
    holder["a"], holder["b"] = Lazy(container, A), Lazy(container, B)
    first, box_a = _run(lambda: holder["a"].value)
    second, box_b = _run(lambda: holder["b"].value)
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive(), "two Lazy values hung on each other"
    errors = [box.get("error") for box in (box_a, box_b)]
    assert any(isinstance(error, CircularDependencyError) for error in errors)


# ---- Y2: closing a scope waits for a creation that waits for the closing thread -----------

def test_y2_close_waiting_for_a_creation_is_reported(caplog):
    container = Container()
    container.creation_wait_warning = 0.2
    released = threading.Event()
    entered = threading.Event()

    def make(c):
        entered.set()
        released.wait(5)                     # set only after close() returns: a hidden wait
        return "made"

    container.register_factory(str, make, LifetimeScope.SCOPED)
    scope = container.create_scope()
    scope.__enter__()
    context = contextvars.copy_context()
    creator, _box = _run(lambda: context.run(container.resolve, str))
    entered.wait(2)
    timer = threading.Timer(1.0, released.set)
    timer.start()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        closer, _ = _run(lambda: scope.state.close())
        assert _wait_for(lambda: _warnings(caplog, "has waited", "str"), 0.9)
        closer.join(3)
    creator.join(3)
    timer.cancel()
    released.set()
    assert not closer.is_alive() and not creator.is_alive()


# ---- Y3 = Z2: the wait warning must not wait behind the deadlock it reports ----------------

def test_y3_a_waiting_thread_that_holds_a_lock_still_reports_at_once(caplog):
    container = Container()
    container.creation_wait_warning = 0.2
    holding_b = threading.Event()
    a_done = threading.Event()

    class B:
        def __init__(self):
            holding_b.set()
            a_done.wait(2.5)                 # T2 waits for A through an Event: invisible

    class A:
        def __init__(self, b: B):
            self.b = b

    container.register_singleton(B)
    container.register_singleton(A)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        t2, _ = _run(lambda: container.resolve(B))
        holding_b.wait(2)
        t1, _ = _run(lambda: container.resolve(A))   # holds A's lock while it waits for B
        reported = _wait_for(lambda: _warnings(caplog, "has waited", "B"), 1.5)
        a_done.set()
        t1.join(3)
        t2.join(3)
    assert reported, "the warning stayed behind the hang"
    assert not t1.is_alive() and not t2.is_alive()


def test_y3_a_blocked_log_handler_never_blocks_the_waiting_thread():
    container = Container()
    container.creation_wait_warning = 0.1
    stuck = threading.Event()
    entered = threading.Event()

    class Stuck(logging.Handler):
        def emit(self, record):
            if "has waited" in record.getMessage():
                stuck.wait(5)

    class Slow:
        def __init__(self):
            entered.set()
            time.sleep(0.5)

    handler = Stuck()
    logging.getLogger("py_autowired").addHandler(handler)
    try:
        container.register_singleton(Slow)
        first, _ = _run(lambda: container.resolve(Slow))
        entered.wait(2)
        second, box = _run(lambda: container.resolve(Slow))   # waits > 0.1 s: a warning
        second.join(3)
        assert not second.is_alive(), "the waiting thread was blocked by a log handler"
        assert isinstance(box.get("result"), Slow)
    finally:
        stuck.set()
        logging.getLogger("py_autowired").removeHandler(handler)
        first.join(3)


def test_y3_a_full_warning_queue_drops_the_oldest_and_says_how_many(caplog):
    writer_class = getattr(container_module, "_BackgroundLog", None)
    assert writer_class is not None
    gate = threading.Event()

    class Gate(logging.Handler):
        def emit(self, record):
            gate.wait(5)

    handler = Gate()
    logger = logging.getLogger("py_autowired")
    logger.addHandler(handler)
    writer = writer_class(size=2)
    try:
        with caplog.at_level(logging.DEBUG, logger="py_autowired"):
            writer.put("first %s", (1,))
            time.sleep(0.2)                                # the writer is stuck on "first"
            for number in range(2, 7):
                writer.put("message %s", (number,))
            gate.set()
            assert _wait_for(lambda: _warnings(caplog, "message 6"))
    finally:
        gate.set()
        logger.removeHandler(handler)
    text = [r.getMessage() for r in caplog.records]
    assert any("3 py_autowired warnings were dropped" in line for line in text)
    assert "message 5" in text and "message 6" in text


# ---- Y4: the library lock does not wait silently ----------------------------------------

def test_y4_waiting_for_the_library_lock_is_reported(monkeypatch, caplog):
    monkeypatch.setattr(autowired, "_GLOBAL_WAIT_WARNING", 0.2, raising=False)
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder, _ = _run(hold)
    held.wait(2)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        waiter, _ = _run(autowired.refresh)
        reported = _wait_for(lambda: _warnings(caplog, "py_autowired lock"), 1.5)
        release.set()
        waiter.join(3)
        holder.join(3)
    assert reported
    assert not waiter.is_alive() and not holder.is_alive()


# ---- projects for the late-import findings ------------------------------------------------

@pytest.fixture
def project(tmp_path, monkeypatch):
    tags = []

    def build(files, validate=False, app_tail="", container="Container()"):
        tag = uuid.uuid4().hex[:8]
        tags.append(tag)
        root = tmp_path / f"r3_{tag}"
        root.mkdir()
        (root / "requirements.txt").write_text("")
        (root / f"clock_{tag}.py").write_text("class Clock:\n    pass\n")
        (root / f"app_{tag}.py").write_text(textwrap.dedent(f"""
            from pathlib import Path
            from py_autowired import Container, autowired
            from clock_{tag} import Clock
            container = {container}
            container.register_singleton(Clock)
            autowired.auto_inject(container, root_dir=str(Path(__file__).resolve().parent), validate={validate})
            app = "ready"
        """))
        for name, body in files.items():
            path = root / name.replace("TAG", tag)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(body).replace("TAG", tag))
        monkeypatch.syspath_prepend(str(root))
        monkeypatch.syspath_prepend(str(root / "dist"))
        return root, tag

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    before = set(sys.modules)
    yield build
    for name in set(sys.modules) - before:
        if any(tag in name for tag in tags):
            sys.modules.pop(name, None)
    for state in ("_DEFERRED", "_AWAITING"):
        pending = getattr(autowired, state, {})
        for name in list(pending):
            if any(tag in name for tag in tags):
                pending.pop(name, None)


MISSING = "class Consumer:\n    def __init__(self):\n        self.missing_thing_instance = None\n"


def _import(name):
    try:
        return importlib.import_module(name), None
    except Exception as error:  # noqa: BLE001
        return None, error


# ---- Y5: an import queued while the library is busy is reported ---------------------------

def test_y5_a_queued_import_is_a_warning(project, caplog):
    _root, tag = project({"dist/late_TAG.py": "class Late:\n    clock_instance = None\n"})
    importlib.import_module(f"app_{tag}")
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder, _ = _run(hold)
    held.wait(2)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        try:
            importlib.import_module(f"late_{tag}")
        finally:
            release.set()
            holder.join(3)
        assert _wait_for(lambda: _warnings(caplog, f"late_{tag}", "busy"))


# ---- Y6 = Z3: with validate=True every late project module is checked ---------------------

def test_y6_with_validate_a_late_module_is_checked_when_it_loads(project):
    _root, tag = project({"dist/late_TAG.py": MISSING}, validate=True)
    importlib.import_module(f"app_{tag}")
    module, error = _import(f"late_{tag}")
    assert module is None and isinstance(error, AutowiringValidationError)
    assert any("missing_thing_instance: no matching registration" in issue for issue in error.issues)


def test_y6_control_without_validate_a_late_module_only_warns_at_use(project, caplog):
    _root, tag = project({"dist/late_TAG.py": MISSING})
    importlib.import_module(f"app_{tag}")
    module, error = _import(f"late_{tag}")
    assert error is None and module is not None


class _NoWeakSet:
    """What a WeakSet does with a container that cannot be weakly referenced."""

    def add(self, item):
        raise TypeError("cannot create weak reference")

    def discard(self, item):
        raise TypeError("cannot create weak reference")

    def __contains__(self, item):
        return False


def test_a9_validate_is_kept_for_a_container_that_cannot_be_weakly_referenced(project, monkeypatch):
    monkeypatch.setattr(autowired, "_VALIDATING", _NoWeakSet())
    _root, tag = project({"dist/late_TAG.py": MISSING}, validate=True)
    importlib.import_module(f"app_{tag}")
    module, error = _import(f"late_{tag}")
    assert module is None and isinstance(error, AutowiringValidationError), "the late module was not checked"


def test_a9_a_later_auto_inject_without_validate_clears_it(project, monkeypatch):
    monkeypatch.setattr(autowired, "_VALIDATING", _NoWeakSet())
    _root, tag = project({"dist/late_TAG.py": MISSING}, validate=True)
    app = importlib.import_module(f"app_{tag}")
    autowired.auto_inject(app.container, root_dir=str(_root), validate=False)
    module, error = _import(f"late_{tag}")
    assert error is None and module is not None


UNHASHABLE = "type('UnhashableContainer', (Container,), {'__eq__': lambda s, o: s is o, '__hash__': None})()"


def test_t3_2_validate_is_kept_for_a_real_unhashable_container(project):
    """Third review: a real container class with __hash__ = None (it CAN be weakly referenced)."""
    _root, tag = project({"dist/late_TAG.py": MISSING}, validate=True, container=UNHASHABLE)
    app = importlib.import_module(f"app_{tag}")
    assert type(app.container).__hash__ is None
    module, error = _import(f"late_{tag}")
    assert isinstance(error, AutowiringValidationError), f"late import broke or was not checked: {error!r}"


def test_t3_2_an_unhashable_container_without_validate_imports_late_modules(project):
    _root, tag = project({"dist/late_TAG.py": MISSING}, container=UNHASHABLE)
    importlib.import_module(f"app_{tag}")
    module, error = _import(f"late_{tag}")
    assert error is None and module is not None, f"late import broke: {error!r}"


# ---- Z4 (Y6b): ties created after startup are reported by refresh() -----------------------

def test_z4_refresh_reports_a_tie_created_after_startup(graph, caplog):
    container, patch = graph

    class IStoreZ4(abc.ABC):
        @abc.abstractmethod
        def get(self): ...

    class IOtherZ4(abc.ABC):
        @abc.abstractmethod
        def other(self): ...

    class SqlStoreZ4(IStoreZ4, IOtherZ4):
        def get(self):
            return 1

        def other(self):
            return 2

    container.register_singleton(IStoreZ4, SqlStoreZ4)
    consumer = type("ConsumerZ4", (), {"__module__": __name__, "sql_store_z4_instance": None})
    patch(consumer)
    container.register_singleton(IOtherZ4, SqlStoreZ4)     # after startup: a second, equal candidate
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.refresh()
        autowired.refresh()                                # reported once, not on every refresh
    assert len(_warnings(caplog, "equally good candidates", "ConsumerZ4.sql_store_z4_instance")) == 1


# ---- Y7 = Z5: a deferred file's real import failure fails with validate=True ---------------

def test_y7_a_deferred_file_that_cannot_import_fails_the_first_injection(project):
    _root, tag = project({
        f"main_TAG.py": "from app_TAG import app\nimport does_not_exist_pkg_y7\n",
        "other_TAG.py": "class Other:\n    clock_instance = None\n",
    }, validate=True)
    importlib.import_module(f"app_{tag}")
    other = sys.modules[f"other_{tag}"]
    try:
        other.Other()
        error = None
    except Exception as caught:  # noqa: BLE001
        error = caught
    assert isinstance(error, AutowiringValidationError)
    assert any(f"main_{tag}: could not be imported" in issue for issue in error.issues)


def test_n10_without_validate_a_later_import_failure_is_a_validation_issue(project):
    _root, tag = project({
        f"main_TAG.py": "from app_TAG import app\nimport does_not_exist_pkg_n10\n",
        "other_TAG.py": "class Other:\n    clock_instance = None\n",
    })
    app = importlib.import_module(f"app_{tag}")
    sys.modules[f"other_{tag}"].Other()                    # the first injection loads the skipped file
    try:
        validate_autowiring(app.container)
        issues = ()
    except AutowiringValidationError as error:
        issues = error.issues
    assert any(f"main_{tag}: could not be imported after startup" in issue for issue in issues), issues


# ---- Y8 = Z7: Lazy[Transient] -> Scoped held by a singleton --------------------------------

class SessionY8:
    pass


class MiddleY8:
    def __init__(self, session: SessionY8):
        self.session = session


class HolderY8:
    def __init__(self, middle: Lazy[MiddleY8]):
        self.middle = middle


def test_y8_an_indirect_scoped_capture_through_lazy_is_reported(graph):
    container, patch = graph
    container.register_scoped(SessionY8)
    container.register_transient(MiddleY8)
    container.register_singleton(HolderY8)
    patch(type("ConsumerY8", (), {"__module__": __name__, "holder_y8_instance": None}))
    found = [issue for issue in _issues(container) if "captures scoped SessionY8" in issue]
    assert found


# ---- Y9: validation substitutes TypeVars like the container --------------------------------

class SessionY9:
    pass


class BoxY9(Generic[T]):
    def __init__(self, dependency: T):
        self.dependency = dependency


class UserY9:
    def __init__(self, box: BoxY9[SessionY9]):
        self.box = box


def test_y9_a_typevar_parameter_is_checked_with_its_real_type(graph):
    container, patch = graph
    container.register_scoped(SessionY9)
    container.register_open_generic(BoxY9, BoxY9, LifetimeScope.SINGLETON)
    container.register_transient(UserY9)
    patch(type("ConsumerY9", (), {"__module__": __name__, "user_y9_instance": None}))
    issues = _issues(container)
    assert not [issue for issue in issues if "unregistered dependency (~T)" in issue]
    assert [issue for issue in issues if "captures scoped SessionY9" in issue]


# ---- Z8: Provider/Lazy of a closed generic is not "unregistered" ---------------------------

class RepoZ8(Generic[T]):
    pass


class UserZ8:
    pass


class ServiceZ8:
    def __init__(self, repos: Provider[RepoZ8[UserZ8]]):
        self.repos = repos


def test_z8_a_deferred_open_generic_is_not_unregistered(graph):
    container, patch = graph
    container.register_open_generic(RepoZ8)
    container.register_transient(ServiceZ8)
    patch(type("ConsumerZ8", (), {"__module__": __name__, "service_z8_instance": None}))
    assert not [issue for issue in _issues(container) if "defers an unregistered" in issue]


# ---- Y11: injected() of a class that was never patched is a WARNING ------------------------

def test_y11_injected_names_a_field_it_cannot_fill(caplog):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    class NeverPatched:
        def __init__(self):
            self.clock_instance = None

    class _State:
        py_autowired_container = Container()

    class _App:
        state = _State()

    class _Request:
        app = _App()

    autowired.activate_container(_State.py_autowired_container)  # what its startup does
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        asyncio.run(injected(NeverPatched)(_Request()))
        asyncio.run(injected(NeverPatched)(_Request()))
    assert "__ai_ioc_init__" not in NeverPatched.__dict__              # 0.5.4 A3: the class is not touched
    assert len(_warnings(caplog, "NeverPatched.clock_instance", "no matching registration")) == 1


# ---- Y12 = Z10: a later assignment with no registration is a WARNING -----------------------

def test_y12_an_assignment_after_construction_with_no_registration_warns(graph, caplog):
    container, patch = graph

    class LaterY12:
        def __init__(self):
            self.repo_y12_instance = object()

    patch(LaterY12)
    item = LaterY12()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        item.repo_y12_instance = None
    assert len(_warnings(caplog, "LaterY12.repo_y12_instance", "no matching registration")) == 1


# ---- Y13: an import failure keeps its traceback -----------------------------------------

def test_y13_a_discovery_import_failure_keeps_its_traceback(tmp_path, monkeypatch, caplog):
    monkeypatch.syspath_prepend(str(tmp_path))
    (tmp_path / f"broken_y13_{uuid.uuid4().hex[:6]}.py").write_text(
        "def boom():\n    raise RuntimeError('inner cause')\ntry:\n    boom()\nexcept RuntimeError as e:\n"
        "    raise ValueError('outer') from e\n")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired._discover_and_load_modules(tmp_path)
    records = [r for r in caplog.records if "Import failed for broken_y13" in r.getMessage()]
    assert len(records) == 1 and records[0].exc_info and records[0].exc_info[0] is ValueError


# ---- Y14 = Z6: a deferred module whose check failed is reported by validation -----------

def test_y14_a_failed_deferred_check_is_a_validation_issue(project, monkeypatch):
    _root, tag = project({"main_TAG.py": "from app_TAG import app\n" + MISSING})
    app = importlib.import_module(f"app_{tag}")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "meta_path", [f for f in sys.meta_path
                                         if not isinstance(f, autowired._LateImportFinder)])
        importlib.import_module(f"main_{tag}")              # around the hook: not patched yet

    def broken(*_args):
        raise RuntimeError("cannot patch")

    monkeypatch.setattr(autowired, "_patch_module_classes", broken)
    try:
        autowired.validate_autowiring(app.container)
        error = None
    except AutowiringValidationError as caught:
        error = caught
    assert error is not None and any(f"main_{tag}: could not be checked" in issue for issue in error.issues)


# ---- Y15 = Z14: Django ASGI cleanup error reaches the caller even after a second cancel ----

def test_y15_cleanup_error_is_not_lost_after_repeated_cancellation():
    from py_autowired._web import AsgiRequestScope

    class Session:
        async def aclose(self):
            await asyncio.sleep(0.2)
            raise RuntimeError("cleanup failed")

    container = Container()
    container.register_scoped(Session)

    async def application(scope, receive, send):
        container.resolve(Session)

    class _Ready:
        def ensure(self):
            pass

    wrapped = AsgiRequestScope(application, container, _Ready())

    autowired.activate_container(container)  # what the real startup does

    async def main():
        task = asyncio.ensure_future(wrapped({"type": "http"}, None, None))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()                                       # a second cancellation during cleanup
        try:
            await task
        except BaseException as error:  # noqa: BLE001
            return error
        return None

    error = asyncio.run(main())
    assert isinstance(error, RuntimeError) and "cleanup failed" in str(error)


# ---- Y16: a failing metadata pass is a hint failure, never an unkeyed fallback (0.5.4 A2) ----

def test_y16_a_failed_metadata_pass_is_reported_as_a_hint_failure(monkeypatch, caplog):
    real = container_module.get_type_hints

    def flaky(obj, *args, **kwargs):
        if kwargs.get("include_extras"):
            raise NameError("extras failed")
        return real(obj, *args, **kwargs)

    monkeypatch.setattr(container_module, "get_type_hints", flaky)

    class DepY16:
        pass

    class UsesY16:
        def __init__(self, dep: DepY16):
            self.dep = dep

    container = Container()
    container.register_transient(DepY16)
    container.register_transient(UsesY16)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        assert isinstance(container.resolve(UsesY16).dep, DepY16)      # the real annotation is used
    assert len(_warnings(caplog, "UsesY16", "type hints cannot be resolved")) == 1


# ---- Z13: if both the note and the log fail, the error still reaches stderr ---------------

def test_z13_a_disposal_error_is_never_lost_completely(capsys):
    class NoNotes(Exception):
        def add_note(self, note):
            raise RuntimeError("no notes")

        def __setattr__(self, name, value):
            if name == "__notes__":
                raise RuntimeError("no notes")
            super().__setattr__(name, value)

    class Exploding(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler broke")

    handler = Exploding()
    logger = logging.getLogger("py_autowired")
    logger.addHandler(handler)
    try:
        with pytest.raises(NoNotes):
            container_module._raise_disposal_errors([NoNotes("first"), KeyError("second")])
    finally:
        logger.removeHandler(handler)
    assert "Additional disposal error: KeyError" in capsys.readouterr().err


# ---- Z15: a container without weak references keeps its issues to itself -----------------

def test_z15_a_non_weakrefable_container_is_pinned_while_it_has_entries():
    class Duck:
        __slots__ = ("registrations",)

        def __init__(self):
            self.registrations = {}

    duck = Duck()
    autowired._record_issue(duck, "duck issue")
    try:
        pinned = getattr(autowired, "_PINNED", {})
        assert pinned.get(id(duck)) is duck            # its id cannot be reused by another object
    finally:
        autowired._UNTRACKED.pop(id(duck), None)
        getattr(autowired, "_PINNED", {}).pop(id(duck), None)


# ---- branch guards for the round-3 code -----------------------------------------------------

def test_guard_no_stderr_when_the_note_or_the_log_works(capsys):
    with pytest.raises(ValueError):
        container_module._raise_disposal_errors([ValueError("first"), KeyError("second")])
    assert capsys.readouterr().err == ""

    class NoNotes(Exception):
        def add_note(self, note):
            raise RuntimeError("no notes")

        def __setattr__(self, name, value):
            if name == "__notes__":
                raise RuntimeError("no notes")
            super().__setattr__(name, value)

    with pytest.raises(NoNotes):                         # the log still works: no stderr
        container_module._raise_disposal_errors([NoNotes("first"), KeyError("second")])
    assert capsys.readouterr().err == ""


def test_guard_no_keyed_warning_when_type_hints_already_failed(caplog):
    container = Container()

    class BrokenHintsR3:
        def __init__(self, dep: "UndefinedR3" = None):  # noqa: F821
            self.dep = dep

    container.register_transient(BrokenHintsR3)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        container.resolve(BrokenHintsR3)
    assert _warnings(caplog, "BrokenHintsR3", "Keyed") == []


def test_guard_a_short_wait_for_the_library_lock_is_not_reported(monkeypatch, caplog):
    monkeypatch.setattr(autowired, "_GLOBAL_WAIT_WARNING", 5.0, raising=False)
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder, _ = _run(hold)
    held.wait(2)
    threading.Timer(0.3, release.set).start()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.refresh()
        time.sleep(0.2)
    holder.join(3)
    assert _warnings(caplog, "py_autowired lock") == []


def test_guard_the_library_lock_warning_names_the_holder_once(monkeypatch, caplog):
    monkeypatch.setattr(autowired, "_GLOBAL_WAIT_WARNING", 0.1, raising=False)
    held, release = threading.Event(), threading.Event()
    box = {}

    def hold():
        with autowired._GLOBAL_LOCK:
            box["ident"] = threading.get_ident()
            held.set()
            release.wait(5)

    holder, _ = _run(hold)
    held.wait(2)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        waiter, _ = _run(autowired.refresh)
        time.sleep(0.8)
        release.set()
        waiter.join(3)
        holder.join(3)
        time.sleep(0.2)
    found = _warnings(caplog, "py_autowired lock")
    assert len(found) == 1 and str(box["ident"]) in found[0]
    assert autowired._GLOBAL_LOCK._owner is None             # cleared on the last release


def test_guard_injected_does_not_warn_for_a_patched_class(graph, caplog):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected
    container, patch = graph

    class PatchedR3:
        def __init__(self):
            self.clock_instance = None

    patch(PatchedR3)

    class _State:
        py_autowired_container = container

    class _App:
        state = _State()

    class _Request:
        app = _App()

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        asyncio.run(injected(PatchedR3)(_Request()))
    assert _warnings(caplog, "PatchedR3", "not patched") == []


class CycleAR3:
    def __init__(self, b: "Lazy[CycleBR3]"):
        self.b = b


class CycleBR3:
    def __init__(self, a: CycleAR3):
        self.a = a


def test_guard_a_cycle_broken_by_lazy_is_not_an_issue(graph):
    container, patch = graph
    CycleAR3.__init__.__annotations__["b"] = Lazy[CycleBR3]
    container.register_singleton(CycleAR3)
    container.register_transient(CycleBR3)
    patch(type("ConsumerCycleR3", (), {"__module__": __name__, "cycle_a_r3_instance": None}))
    assert _issues(container) == ()


def test_guard_a_cancelled_request_still_reports_its_cancellation():
    from py_autowired._web import AsgiRequestScope

    class Session:
        async def aclose(self):
            await asyncio.sleep(0.1)

    container = Container()
    container.register_scoped(Session)

    async def application(scope, receive, send):
        container.resolve(Session)
        await asyncio.sleep(1)

    class _Ready:
        def ensure(self):
            pass

    wrapped = AsgiRequestScope(application, container, _Ready())

    autowired.activate_container(container)  # what the real startup does

    async def main():
        task = asyncio.ensure_future(wrapped({"type": "http"}, None, None))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return "cancelled"
        return "finished"

    assert asyncio.run(main()) == "cancelled"


def test_guard_a_non_weakrefable_container_is_pinned_for_tracking_too():
    class DuckR3:
        __slots__ = ("registrations",)

        def __init__(self):
            self.registrations = {}

    duck = DuckR3()
    try:
        autowired._remember_patched_class(duck, type("TrackedR3", (), {}))
        assert getattr(autowired, "_PINNED", {}).get(id(duck)) is duck
    finally:
        autowired._PATCHED_CLASSES.pop(id(duck), None)
        getattr(autowired, "_PINNED", {}).pop(id(duck), None)
