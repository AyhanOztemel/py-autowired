"""0.5.4, third blind review: four findings, each pinned by a test that failed before the fix.
(threads are daemons; every join is followed by an "is it still alive" check)"""

import asyncio
import contextvars
import gc
import logging
import threading
import time
import uuid
import weakref

import pytest

from py_autowired import CircularDependencyError, Container, LifetimeScope, ScopeNotActiveError, autowired
from py_autowired import container as container_module


# ---- (1) a failing log handler never stops the background writer -------------------------

def test_t3_1_the_background_writer_survives_a_failing_handler(capsys):
    writer = container_module._BackgroundLog(size=8)
    seen = []

    class Broken(logging.Handler):
        def emit(self, record):
            if "first" in record.getMessage():
                raise RuntimeError("handler broke")
            seen.append(record.getMessage())

    log = logging.getLogger("py_autowired")
    handler = Broken()
    log.addHandler(handler)
    try:
        writer.put("first %s", ("warning",))
        writer.put("second warning", ())
        deadline = time.monotonic() + 3
        while "second warning" not in seen and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        log.removeHandler(handler)
    assert "second warning" in seen, "the writer stopped after a handler failed"
    assert writer._thread.is_alive()
    assert "first warning" in capsys.readouterr().err        # the failed one is not lost either


# ---- (3) a lock cycle found while closing a scope still closes what was created ---------

_REAL_WAIT_CYCLE = container_module._thread_wait_cycle  # taken once: each round wraps this one

def _close_with_a_cycle_found_by_the_closing_thread(monkeypatch, use_async=False):
    closed = []

    if use_async:                              # resources that can only be closed asynchronously
        class Ready:
            async def aclose(self):
                closed.append("Ready")
    else:
        class Ready:
            def close(self):
                closed.append("Ready")

    class Shared:
        pass

    if use_async:
        class Slow:
            def __init__(self, shared: Shared):
                self.shared = shared

            async def aclose(self):
                closed.append("Slow")
    else:
        class Slow:
            def __init__(self, shared: Shared):
                self.shared = shared

            def close(self):
                closed.append("Slow")

    container = Container()
    container.register_scoped(Ready)
    container.register_scoped(Slow)
    scope = container.create_scope()
    box, ids = {}, {}
    in_factory = threading.Event()

    def only_the_closer_sees_it(me):          # forces the order: the closing thread finds the cycle
        return _REAL_WAIT_CYCLE(me) if me == ids.get("a") else None

    monkeypatch.setattr(container_module, "_thread_wait_cycle", only_the_closer_sees_it)

    def close_once():
        if use_async:
            asyncio.run(scope.state.aclose())
        else:
            scope.state.close()

    def make_shared(owner):
        in_factory.set()
        deadline = time.monotonic() + 3
        while ids.get("b") not in container_module._WAITING and time.monotonic() < deadline:
            time.sleep(0.005)                  # B waits for this singleton now
        try:
            close_once()                       # A holds this singleton's creation lock here
            box["close"] = None
        except CircularDependencyError as error:
            box["close"] = error
        box["closed_by_the_closer"] = list(closed)
        return Shared()

    container.register_factory(Shared, make_shared, scope=LifetimeScope.SINGLETON)
    scope.__enter__()
    container.resolve(Ready)

    def thread_a():
        ids["a"] = threading.get_ident()
        container.resolve(Shared)

    def thread_b():
        ids["b"] = threading.get_ident()
        in_factory.wait(3)
        try:
            box["b"] = container.resolve(Slow)
        except Exception as error:  # noqa: BLE001 - the test inspects it
            box["b"] = error

    a = threading.Thread(target=contextvars.copy_context().run, args=(thread_a,), daemon=True)
    b = threading.Thread(target=contextvars.copy_context().run, args=(thread_b,), daemon=True)
    a.start()
    b.start()
    a.join(5)
    b.join(5)
    scope._leave()
    assert not a.is_alive() and not b.is_alive()
    box["open_after"] = not scope.state.closed
    close_once()                               # no lock held now: this close closes everything
    return closed, box


@pytest.mark.parametrize("use_async", [False, True], ids=["close", "aclose"])
def test_t3_3_a_cycle_found_while_closing_runs_no_finalizer_and_leaves_the_scope_whole(monkeypatch, use_async):
    for _ in range(30):
        closed, box = _close_with_a_cycle_found_by_the_closing_thread(monkeypatch, use_async)
        assert isinstance(box["close"], CircularDependencyError)          # the forced path was taken
        assert box["closed_by_the_closer"] == [], "a finalizer ran while a creation lock was held"
        assert box["open_after"], "the scope was left half closed"
        assert type(box["b"]).__name__ == "Slow", f"the creation did not finish normally: {box['b']!r}"
        assert sorted(closed) == ["Ready", "Slow"], f"a scoped object was left open: {closed}"


# ---- (4) injected() fills from the container of its own application ------------------------

class _ClockT3:
    def __init__(self):
        self.owner = None


def _request_for(container):
    state = type("State", (), {"py_autowired_container": container})()
    app = type("App", (), {"state": state})()
    return type("Request", (), {"app": app})()


def _two_applications(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    first, second = Container(), Container()
    for name, container in (("first", first), ("second", second)):
        clock = _ClockT3()
        clock.owner = name
        container.register_instance(_ClockT3, clock)
    return first, second


def test_t3_4_injected_never_fills_from_another_application_s_container(monkeypatch):
    pytest.importorskip("fastapi")
    from py_autowired import DependencyInjectionError
    from py_autowired.fastapi import injected

    first, second = _two_applications(monkeypatch)
    autowired.activate_container(second)                   # the other application started last

    class OutsideT3:
        def __init__(self):
            self.clockt3_instance = None

    try:
        made, error = asyncio.run(injected(OutsideT3)(_request_for(first))), None
    except Exception as caught:  # noqa: BLE001 - the test inspects it
        made, error = None, caught
    assert isinstance(error, DependencyInjectionError), f"filled from another application's container: {made!r}"


def test_t3_4_the_active_container_s_cache_is_used_when_it_is_its_own(monkeypatch):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    first, _second = _two_applications(monkeypatch)
    autowired.activate_container(first)
    autowired._NAME_MAP_CACHE.get_raw_map()
    maps = []
    real_map = autowired._build_name_map_from_container
    monkeypatch.setattr(autowired, "_build_name_map_from_container",
                        lambda owner: maps.append(owner) or real_map(owner))

    class OwnT3:
        def __init__(self):
            self.clockt3_instance = None

    for _ in range(2):
        assert asyncio.run(injected(OwnT3)(_request_for(first))).clockt3_instance.owner == "first"
    assert maps == []


# ---- (5) replace() moves the aliases of the replaced registration with it ---------------------

class _IClockT5:
    pass


class _OldClockT5(_IClockT5):
    pass


class _NewClockT5(_IClockT5):
    pass


class _OtherClockT5(_IClockT5):
    pass


class _AliasT5:
    pass


class _ChainT5:
    pass


def test_t3_5_replace_moves_an_alias_to_the_new_registration():
    container = Container()
    container.register_singleton(_IClockT5, _OldClockT5)
    container.register_alias(_AliasT5, _IClockT5)
    container.replace(_IClockT5, _NewClockT5)
    assert isinstance(container.resolve(_AliasT5), _NewClockT5), "the alias kept the replaced registration"


def test_t3_5_a_chain_of_aliases_follows_too():
    container = Container()
    container.register_singleton(_IClockT5, _OldClockT5)
    container.register_alias(_AliasT5, _IClockT5)
    container.register_alias(_ChainT5, _AliasT5)                # _ChainT5 -> _AliasT5 -> _IClockT5
    container.replace_singleton(_IClockT5, _NewClockT5)
    assert isinstance(container.resolve(_ChainT5), _NewClockT5), "the chained alias kept the replaced registration"
    assert container.resolve(_ChainT5) is container.resolve(_IClockT5)    # one shared singleton, as before


def test_t3_5_an_alias_of_an_earlier_registration_follows_replace():
    container = Container()
    container.register_singleton(_IClockT5, _OldClockT5)
    container.register_alias(_AliasT5, _IClockT5)
    container.register_singleton(_IClockT5, _OtherClockT5)      # not strict: a second registration
    container.replace(_IClockT5, _NewClockT5)                   # replace removes every earlier one
    assert isinstance(container.resolve(_AliasT5), _NewClockT5)


def test_t3_5_control_replacing_the_alias_itself_leaves_the_target():
    container = Container()
    container.register_singleton(_IClockT5, _OldClockT5)
    container.register_alias(_AliasT5, _IClockT5)
    container.replace(_AliasT5, _NewClockT5)
    assert isinstance(container.resolve(_IClockT5), _OldClockT5)
    assert isinstance(container.resolve(_AliasT5), _NewClockT5)


def test_t3_5_control_register_still_leaves_an_alias_where_it_was():
    container = Container()
    container.register_singleton(_IClockT5, _OldClockT5)
    container.register_alias(_AliasT5, _IClockT5)
    container.register_singleton(_IClockT5, _OtherClockT5)      # register, not replace: unchanged behaviour
    assert isinstance(container.resolve(_AliasT5), _OldClockT5)


def test_t3_5_control_keyed_registrations_are_not_touched():
    container = Container()
    container.register_singleton(_IClockT5, _OldClockT5)
    container.register_keyed(_IClockT5, "k", _OtherClockT5)
    container.register_alias(_AliasT5, _IClockT5)
    container.replace(_IClockT5, _NewClockT5)
    assert isinstance(container.resolve_keyed(_IClockT5, "k"), _OtherClockT5)
    assert isinstance(container.resolve(_AliasT5), _NewClockT5)


# ---- mutants of earlier rounds that no test killed (found when the baseline gate was added) --

def test_r3_a_background_warning_is_queued_once(monkeypatch):
    puts = []
    monkeypatch.setattr(container_module._BACKGROUND_LOG, "put",
                        lambda message, args, exc_info=None: puts.append(message))
    key = ("once-test", uuid.uuid4().hex)
    container_module._warn_once_background(key, "once %s", 1)
    container_module._warn_once_background(key, "once %s", 1)
    assert puts == ["once %s"]


def test_r3_no_stderr_when_only_the_log_fails(capsys):
    class Exploding(logging.Handler):
        def emit(self, record):
            raise RuntimeError("handler broke")

    log = logging.getLogger("py_autowired")
    handler = Exploding()
    log.addHandler(handler)
    try:
        with pytest.raises(ValueError) as caught:           # the note works, the log does not
            container_module._raise_disposal_errors([ValueError("first"), KeyError("second")])
    finally:
        log.removeHandler(handler)
    assert any("second" in note for note in getattr(caught.value, "__notes__", ()))
    assert capsys.readouterr().err == ""                  # reported once (the note): no stderr


class _ReadyT6:
    def ensure(self):
        pass


def test_r3_a_cleanup_error_at_the_moment_of_cancellation_reaches_the_caller():
    from py_autowired._web import AsgiRequestScope

    holder = {}

    class Session:
        async def aclose(self):
            holder["request"].cancel()                       # cancelled exactly as the cleanup fails
            raise RuntimeError("cleanup failed")

    container = Container()
    container.register_scoped(Session)

    async def application(scope, receive, send):
        container.resolve(Session)

    wrapped = AsgiRequestScope(application, container, _ReadyT6())

    autowired.activate_container(container)  # what the real startup does

    async def main():
        holder["request"] = asyncio.ensure_future(wrapped({"type": "http"}, None, None))
        try:
            await holder["request"]
        except BaseException as error:  # noqa: BLE001 - the test inspects it
            return error
        return None

    error = asyncio.run(main())
    assert isinstance(error, RuntimeError) and "cleanup failed" in str(error), repr(error)


def test_r3_a_cancellation_during_a_successful_cleanup_still_reaches_the_caller():
    from py_autowired._web import AsgiRequestScope

    holder, closed = {}, []

    class Session:
        async def aclose(self):
            holder["started"].set()
            await holder["release"].wait()
            closed.append(True)

    container = Container()
    container.register_scoped(Session)

    async def application(scope, receive, send):
        container.resolve(Session)

    wrapped = AsgiRequestScope(application, container, _ReadyT6())

    autowired.activate_container(container)  # what the real startup does

    async def main():
        holder["started"], holder["release"] = asyncio.Event(), asyncio.Event()
        request = asyncio.ensure_future(wrapped({"type": "http"}, None, None))
        await asyncio.wait_for(holder["started"].wait(), 5)    # a failing request must not hang the test
        request.cancel()                                     # cancelled while the cleanup runs
        await asyncio.sleep(0)
        holder["release"].set()
        try:
            await request
        except asyncio.CancelledError:
            return "cancelled"
        return "returned"

    assert asyncio.run(main()) == "cancelled"
    assert closed == [True]                                  # and the cleanup finished


def test_t3_4_its_own_lookup_also_loads_skipped_files_first(monkeypatch):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    first, second = _two_applications(monkeypatch)
    autowired.activate_container(first)  # its own container is the active one
    loads = []
    monkeypatch.setattr(autowired, "_AWAITING", {"a_skipped_module": True})
    monkeypatch.setattr(autowired, "_load_skipped", lambda: loads.append(True))

    class PendingT3:
        def __init__(self):
            self.clockt3_instance = None

    asyncio.run(injected(PendingT3)(_request_for(first)))
    assert loads, "the deferred check of a skipped file was bypassed"


def test_t3_4_a_field_that_cannot_be_read_is_still_reported(monkeypatch, caplog):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    first, second = _two_applications(monkeypatch)
    autowired.activate_container(first)  # its own container is the active one

    class BrokenReadT8:
        @property
        def clockt3_instance(self):
            raise RuntimeError("cannot read")

        @clockt3_instance.setter
        def clockt3_instance(self, value):
            pass

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        asyncio.run(injected(BrokenReadT8)(_request_for(first)))
    assert any("Field read failed" in r.getMessage() and "BrokenReadT8.clockt3_instance" in r.getMessage()
               for r in caplog.records)


# ---- seventh review: nothing to fill -> nothing is loaded; a failing read is reported once -----

def test_t3_4_an_object_with_full_fields_loads_no_skipped_file(monkeypatch):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected
    from py_autowired import AutowiringValidationError

    first, second = _two_applications(monkeypatch)
    autowired.activate_container(first)  # its own container is the active one

    def load_skipped():                                      # another application's broken skipped file
        raise AutowiringValidationError(["another_app.main: missing registration"])

    monkeypatch.setattr(autowired, "_AWAITING", {"another_app.main": True})
    monkeypatch.setattr(autowired, "_load_skipped", load_skipped)
    own_clock = _ClockT3()

    class FullT9:
        def __init__(self):
            self.clockt3_instance = own_clock

    try:
        made, error = asyncio.run(injected(FullT9)(_request_for(first))), None  # nothing to fill: nothing to load
    except Exception as caught:  # noqa: BLE001 - the test inspects it
        made, error = None, caught
    assert error is None, f"an unrelated skipped file failed this call: {error!r}"
    assert made.clockt3_instance is own_clock


def test_t3_4_a_read_that_fails_once_is_reported(monkeypatch, caplog):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    first, second = _two_applications(monkeypatch)
    autowired.activate_container(first)  # its own container is the active one
    reads = []

    class FlakyT9:
        @property
        def clockt3_instance(self):
            reads.append(1)
            if len(reads) == 1:
                raise RuntimeError("first read fails")
            return None

        @clockt3_instance.setter
        def clockt3_instance(self, value):
            pass

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        asyncio.run(injected(FlakyT9)(_request_for(first)))
    failed = [r for r in caplog.records if "Field read failed" in r.getMessage()
              and "FlakyT9.clockt3_instance" in r.getMessage()]
    assert len(failed) == 1, "the failing read was not reported"


def test_t3_4_a_read_that_always_fails_is_reported_once_per_call(monkeypatch, caplog):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    first, second = _two_applications(monkeypatch)
    autowired.activate_container(first)  # its own container is the active one

    class AlwaysT9:
        def __init__(self):
            self.clockt3_instance = None                     # a field that is filled

        @property
        def broken_t9_instance(self):                        # and one that can never be read
            raise RuntimeError("never readable")

        @broken_t9_instance.setter
        def broken_t9_instance(self, value):
            pass

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        made = asyncio.run(injected(AlwaysT9)(_request_for(first)))
    assert made.clockt3_instance.owner == "first"
    failed = [r for r in caplog.records if "Field read failed" in r.getMessage()
              and "AlwaysT9.broken_t9_instance" in r.getMessage()]
    assert len(failed) == 1, f"reported {len(failed)} times"
    assert "field skipped (it keeps its current value)" in failed[0].getMessage()
    assert failed[0].exc_info is not None                    # with the traceback, as in a patched __init__
