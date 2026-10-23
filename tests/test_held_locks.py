"""0.5.4 review round 19 (P1): while this thread holds ANY library lock, no log handler runs in it (a
handler may take locks of its own, or wait for another thread) - a WARNING raised meanwhile is
written when the last library lock is released. The count of locks held is the measure, not the
resolution depth: a resolution that takes no lock (a transient) pays nothing."""

import ast
import logging
import threading
from pathlib import Path

import pytest

from py_autowired import Container, Lazy, autowired
from py_autowired import container as container_module
from py_autowired._web import Startup


class _Order(logging.Handler):
    """Records, for each of our test messages, whether it was written while the test held a lock."""

    def __init__(self, inside):
        super().__init__(logging.WARNING)
        self.inside = inside
        self.seen = []

    def emit(self, record):
        if record.getMessage().startswith("held-lock test"):
            self.seen.append(self.inside[0])


@pytest.fixture
def order(monkeypatch):
    monkeypatch.setattr(container_module, "_WARNED", set())
    inside = [False]
    handler = _Order(inside)
    logger = logging.getLogger("py_autowired")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.WARNING)
    yield inside, handler
    logger.removeHandler(handler)
    logger.setLevel(previous)


def _warn(tag):
    container_module._warn_once(("held-lock test", tag), "held-lock test %s", tag)


def _locks():
    container = Container()
    scope = container.create_scope()
    return {
        "global": autowired._GLOBAL_LOCK,
        "registration": container._registration_lock,
        "provider": Container._provider_lock,
        "scope state": scope.state.lock,
        "name map": autowired._CachedNameMap(container)._lock,
        "thread birth": container_module._ThreadBirth._lock,
        "startup": Startup(container, None, None, False)._lock,
    }


@pytest.mark.parametrize("kind", list(_locks()))
def test_no_warning_is_written_while_a_library_lock_is_held(kind, order):
    inside, handler = order
    lock = _locks()[kind]
    with lock:
        inside[0] = True
        _warn(kind)
        assert handler.seen == [], f"written under the {kind} lock"
        inside[0] = False
    assert handler.seen == [False], f"{kind}: kept, then written once after the release"
    assert container_module._HELD.count == 0


def test_no_warning_is_written_under_a_singleton_s_creation_lock(order):
    inside, handler = order
    seen_inside = []

    def factory(container):
        inside[0] = True
        _warn("singleton")
        seen_inside.append(list(handler.seen))
        inside[0] = False
        return object()

    container = Container()
    container.register_factory(object, factory, container_module.LifetimeScope.SINGLETON)
    container.resolve(object)
    assert seen_inside == [[]]
    assert handler.seen == [False]


def test_no_warning_is_written_under_a_scope_gate(order):
    inside, handler = order

    class Scoped:
        def __init__(self):
            inside[0] = True
            _warn("scoped")
            inside[0] = False

    container = Container()
    container.register_scoped(Scoped)
    with container.create_scope():
        container.resolve(Scoped)
    assert handler.seen == [False]


def test_no_warning_is_written_under_a_lazy_gate(order):
    inside, handler = order

    class Built:
        def __init__(self):
            inside[0] = True
            _warn("lazy")
            inside[0] = False

    container = Container()
    container.register_transient(Built)
    Lazy(container, Built).value
    assert handler.seen == [False]


def test_a_transient_without_a_lock_writes_at_once(order):
    """The behaviour change P1 accepted: no library lock held - the WARNING is written at once."""
    inside, handler = order

    class Plain:
        def __init__(self):
            inside[0] = True
            _warn("transient")
            inside[0] = False

    container = Container()
    container.register_transient(Plain)
    container.resolve(Plain)
    assert handler.seen == [True]


def test_the_count_follows_the_creation_lock_owners():
    """_LOCK_OWNERS and the held count agree: inside a singleton's creation this thread owns its
    lock and counts it; afterwards neither."""
    seen = []

    class Service:
        def __init__(self):
            me = threading.get_ident()
            with container_module._WAIT_LOCK:
                owns = any(owner[0] == me for owner in container_module._LOCK_OWNERS.values())
            seen.append((container_module._HELD.count, owns))

    container = Container()
    container.register_singleton(Service)
    container.resolve(Service)
    assert seen == [(1, True)]
    assert container_module._HELD.count == 0


def test_a_release_this_thread_never_counted_keeps_the_count_at_zero(order):
    """A creation lock taken outside _acquire_creation (tests do it) and released through
    _release_creation never makes the count negative - a negative count would keep every later
    WARNING forever."""
    inside, handler = order
    container = Container()
    container.register_singleton(object)
    registration = container.registrations[object]
    registration.lock.acquire()
    container._release_creation(registration)
    container_module._unhold()
    assert container_module._HELD.count == 0
    _warn("after")
    assert handler.seen == [False]


LEAF_LOCKS = {"_WARNED_LOCK", "_WAIT_LOCK", "_change_lock", "_count", "_start"}
LOG_CALLS = {"_warn_once", "_warn_once_background", "warning", "error", "info", "debug",
             "exception", "critical", "log", "_flush_warnings"}


def test_nothing_is_logged_under_a_leaf_lock():
    """The locks that are not counted are leaves: only dictionary and set operations run under
    them - no log call, directly, in any `with <leaf lock>:` block of the library."""
    source = Path(container_module.__file__).parent
    offences = []
    for path in sorted(source.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            names = {getattr(item.context_expr, "attr", getattr(item.context_expr, "id", None))
                     for item in node.items}
            if not names & LEAF_LOCKS:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    called = getattr(inner.func, "attr", getattr(inner.func, "id", None))
                    if called in LOG_CALLS:
                        offences.append(f"{path.name}:{inner.lineno} {called} under {names & LEAF_LOCKS}")
    assert offences == []


def test_a_release_it_never_counted_leaves_the_other_held_locks_counted():
    """HL3: a creation lock this thread never noted (taken outside _acquire_creation) does not
    lower the count of the locks it really holds - else a WARNING would be written under them."""
    container = Container()
    container.register_singleton(object)
    registration = container.registrations[object]
    with autowired._GLOBAL_LOCK:
        registration.lock.acquire()
        container._release_creation(registration)
        assert container_module._HELD.count == 1
    assert container_module._HELD.count == 0


def test_a_contended_creation_lock_counts_once_taken():
    """HL16: a creation lock taken after waiting (the polling path) counts as held too."""
    container = Container()
    container.register_singleton(object)
    registration = container.registrations[object]
    taken, done = threading.Event(), threading.Event()

    def holder():
        registration.lock.acquire()
        taken.set()
        done.wait(0.3)
        registration.lock.release()

    thread = threading.Thread(target=holder)
    thread.start()
    taken.wait(5)
    try:
        container._acquire_creation(registration)  # waits for the holder, then takes it
        assert container_module._HELD.count == 1
        container._release_creation(registration)
        assert container_module._HELD.count == 0
    finally:
        done.set()
        thread.join(5)



# ---- review round 20: every log call of the library respects the held locks ----

def test_a_direct_log_call_is_kept_while_a_library_lock_is_held(order):
    """Y, Z (round 20): direct logger calls (an abstract registration without implementation, a
    field that cannot be read, ...) bypassed the held-lock rule. The library's logger itself now
    keeps every record while a library lock is held."""
    inside, handler = order
    with autowired._GLOBAL_LOCK:
        inside[0] = True
        autowired.logger.warning("held-lock test %s", "direct")
        container_module.logger.error("held-lock test %s", "direct error")
        assert handler.seen == [], "written under the library lock"
        inside[0] = False
    assert handler.seen == [False, False]


def test_a_field_read_failure_is_not_written_under_a_creation_lock(order, caplog):
    """Z (round 20): the post-init 'Field read failed' WARNING was written under the singleton's
    creation lock."""
    inside, handler = order

    class Broken:
        clock_instance = property(lambda self: 1 / 0, lambda self, value: None)

    seen_inside = []

    class Probe(logging.Handler):
        def emit(self, record):
            if "Field read failed" in record.getMessage():
                seen_inside.append(container_module._HELD.count)

    probe = Probe(logging.WARNING)
    logging.getLogger("py_autowired").addHandler(probe)
    try:
        container = Container()
        container.register_singleton(Broken)
        autowired._patch_init(Broken, container, {})
        container.resolve(Broken)
    finally:
        logging.getLogger("py_autowired").removeHandler(probe)
    assert seen_inside == [0], f"written while {seen_inside} library locks were held"


def test_the_library_logs_only_through_its_held_lock_logger():
    """No log call in the library can bypass the held-lock rule: both modules' `logger` is the
    held-lock logger, and no module calls logging.getLogger / logging.<level> elsewhere."""
    assert isinstance(autowired.logger, container_module._HeldLogger)
    assert isinstance(container_module.logger, container_module._HeldLogger)
    source = Path(container_module.__file__).parent
    offences = []
    for path in sorted(source.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # The only allowed use: the real logger handed to _HeldLogger(...).
        wrapped = {id(node.args[0]) for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_HeldLogger"
                   and node.args}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "logging" \
                    and node.func.attr in {"getLogger", "debug", "info", "warning", "error",
                                           "exception", "critical", "log"} \
                    and id(node) not in wrapped:
                offences.append(f"{path.name}:{node.lineno} logging.{node.func.attr}")
    assert offences == [], offences



def test_a_kept_record_keeps_its_level_and_its_exception():
    """A record kept under a library lock is written with its own level and the exception of the
    moment it was raised - not of the release."""
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("held-lock test"):
                records.append(record)

    keep = Keep(logging.DEBUG)
    logger = logging.getLogger("py_autowired")
    previous = logger.level
    logger.addHandler(keep)
    logger.setLevel(logging.DEBUG)
    try:
        with autowired._GLOBAL_LOCK:
            try:
                raise KeyError("the original")
            except KeyError:
                autowired.logger.error("held-lock test %s", "exc", exc_info=True)
            assert records == []
    finally:
        logger.removeHandler(keep)
        logger.setLevel(previous)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert records[0].exc_info is not None and records[0].exc_info[0] is KeyError



def _direct_under_the_library_lock(real, returned):
    with autowired._GLOBAL_LOCK:
        real._cache.clear()  # the cache misses: isEnabledFor would take logging's lock
        autowired.logger.warning("held-lock test %s", "logging lock")
        returned.append(True)  # still under the library lock: the call itself did not wait


def _warn_once_under_a_creation_lock(real, returned):
    """Y (round 20 targeted check): the old _warn_once kept its WARNING under a creation lock without
    asking logging anything - it must still do so."""
    container_module._WARNED.discard(("held-lock test", "creation"))

    def factory(container):
        real._cache.clear()
        container_module._warn_once(("held-lock test", "creation"), "held-lock test %s", "creation")
        returned.append(True)  # still under the creation lock: the call itself did not wait
        return object()

    container = Container()
    container.register_factory(object, factory, container_module.LifetimeScope.SINGLETON)
    container.resolve(object)


@pytest.mark.parametrize("call", [_direct_under_the_library_lock, _warn_once_under_a_creation_lock])
def test_a_log_call_under_a_library_lock_never_waits_for_logging_s_own_lock(call):
    """Round 20 (denetçi notu, Y, Z): Logger.isEnabledFor takes logging's module lock when its cache
    misses, and logging.config.dictConfig holds that lock while it calls the user's handler
    factories - which may resolve from the container and wait for a library lock this thread holds.
    A log call under a library lock therefore never asks logging anything: the record is kept, the
    level is checked when it is written (outside the library locks)."""
    real = logging.getLogger("py_autowired")
    holder_has_it, release_it = threading.Event(), threading.Event()

    def hold_loggings_lock():
        logging._lock.acquire()  # what dictConfig holds while it runs the user's factories
        holder_has_it.set()
        release_it.wait(10)
        logging._lock.release()

    returned = []

    def log_under_the_library_lock():
        # After the release the kept record is written - that may wait for logging's lock, outside
        # every library lock (no cycle): only the call under the lock must return at once.
        call(real, returned)

    holder = threading.Thread(target=hold_loggings_lock)
    holder.start()
    holder_has_it.wait(5)
    caller = threading.Thread(target=log_under_the_library_lock)
    try:
        caller.start()
        caller.join(2)
        assert returned == [True], "the log call waited for logging's lock under the library lock"
    finally:
        release_it.set()
        holder.join(10)
        caller.join(10)



@pytest.mark.parametrize("kind", ["registration", "scope state", "provider"])
def test_explicit_acquire_and_release_keep_and_write_like_with(kind, order):
    """HL6b, HL7b, HL7d (gate): the lock's own acquire()/release() keep a WARNING the same way as
    `with` does and write it at the last release; the count comes back to where it was."""
    inside, handler = order
    lock = _locks()[kind]
    before = container_module._HELD.count
    assert lock.acquire()
    try:
        inside[0] = True
        _warn("explicit " + kind)
        assert handler.seen == [], "written under the lock"
        inside[0] = False
    finally:
        lock.release()
    assert handler.seen == [False]
    assert container_module._HELD.count == before
