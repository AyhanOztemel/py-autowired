"""0.5.4: resolving a singleton that is already created returns it at once. It takes no lock and
has nothing to warn about, so the per-call bookkeeping (resolution depth, deferred-warning flush)
is skipped - it made field injection about 50% slower than in 0.5.3."""

import threading

import pytest

from py_autowired import Container, LifetimeScope
from py_autowired import container as container_module
from py_autowired.container import ContainerClosedError


class Clock:
    pass


class Counting:
    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.real(*args, **kwargs)


class DepthWatch:
    """Stands in for the thread's held-locks count and records every write."""

    count = 0  # the real state's per-thread defaults
    pending = None

    def __init__(self):
        self.__dict__["writes"] = []

    def __setattr__(self, name, value):
        self.__dict__["writes"].append((name, value))
        self.__dict__[name] = value


def test_a_ready_singleton_skips_the_resolution_bookkeeping(monkeypatch):
    container = Container()
    container.register_singleton(Clock)
    first = container.resolve(Clock)                       # created here, with the bookkeeping
    flush = Counting(container_module._flush_warnings)
    depth = DepthWatch()
    monkeypatch.setattr(container_module, "_flush_warnings", flush)
    monkeypatch.setattr(container_module, "_HELD", depth)
    for _ in range(3):
        assert container.resolve(Clock) is first
    assert flush.calls == 0, "a ready singleton flushed the deferred warnings"
    assert depth.writes == [], "a ready singleton took a library lock"


def test_a_supplied_instance_is_ready_too(monkeypatch):
    container = Container()
    clock = Clock()
    container.register_instance(Clock, clock)
    flush = Counting(container_module._flush_warnings)
    monkeypatch.setattr(container_module, "_flush_warnings", flush)
    assert container.resolve(Clock) is clock
    assert flush.calls == 0


def test_a_singleton_not_created_yet_holds_its_creation_lock(monkeypatch):
    """Round 19 (P1): the bookkeeping is the count of library locks held - a singleton's creation
    lock counts while it is held, and only then."""
    container = Container()
    container.register_singleton(Clock)
    depth = DepthWatch()
    monkeypatch.setattr(container_module, "_HELD", depth)
    container.resolve(Clock)
    assert depth.writes == [("count", 1), ("count", 0)]


def test_a_transient_takes_no_library_lock(monkeypatch):
    """Round 19 (P1): a transient takes no library lock, so it pays no bookkeeping (it was the
    largest part of injected()'s slowdown against 0.5.3)."""
    container = Container()
    container.register_transient(Clock)
    depth = DepthWatch()
    monkeypatch.setattr(container_module, "_HELD", depth)
    container.resolve(Clock)
    container.resolve(Clock)
    assert depth.writes == []


def test_a_scoped_creation_counts_its_locks_and_ends_at_zero(monkeypatch):
    container = Container()
    container.register_scoped(Clock)
    with container.create_scope():
        depth = DepthWatch()
        monkeypatch.setattr(container_module, "_HELD", depth)
        container.resolve(Clock)
        container.resolve(Clock)  # already created in this scope: no lock
    counts = [value for name, value in depth.writes if name == "count"]
    assert counts and max(counts) >= 1 and counts[-1] == 0, depth.writes


def test_a_deferred_warning_is_still_flushed(monkeypatch, caplog):
    """Round 19 (speed): a WARNING raised during a creation is written once the outermost
    resolution ends - the flush runs whenever something is pending."""
    import logging

    monkeypatch.setattr(container_module, "_WARNED", set())

    class Noisy:
        def __init__(self):
            container_module._warn_once(("noisy-test",), "noisy %s", "created")

    container = Container()
    container.register_transient(Noisy)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        container.resolve(Noisy)
    assert [r.getMessage() for r in caplog.records if r.getMessage() == "noisy created"] == ["noisy created"]
    assert not container_module._HELD.pending


def test_a_closed_container_still_refuses_a_ready_singleton():
    container = Container()
    clock = Clock()
    container.register_instance(Clock, clock)
    assert container.resolve(Clock) is clock
    container.close()
    with pytest.raises(ContainerClosedError):
        container.resolve(Clock)


def test_a_replaced_singleton_is_the_new_one():
    class NewClock(Clock):
        pass

    container = Container()
    container.register_singleton(Clock)
    old = container.resolve(Clock)
    container.replace_singleton(Clock, NewClock)
    new = container.resolve(Clock)
    assert type(new) is NewClock and new is not old
    assert container.resolve(Clock) is new


def test_many_threads_read_one_ready_singleton():
    container = Container()
    container.register_singleton(Clock)
    first = container.resolve(Clock)
    barrier = threading.Barrier(8)
    seen = []

    def read():
        barrier.wait()
        for _ in range(2000):
            seen.append(container.resolve(Clock))

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not any(thread.is_alive() for thread in threads)
    assert len(seen) == 16000 and all(item is first for item in seen)


def test_only_a_singleton_answers_with_its_stored_instance():
    """ServiceRegistration is public: a transient one carrying an instance still builds anew."""
    from py_autowired import ServiceRegistration

    container = Container()
    stored = Clock()
    container.registrations[Clock] = ServiceRegistration(
        service_type=Clock, implementation_type=Clock, scope=LifetimeScope.TRANSIENT, instance=stored)
    made = container.resolve(Clock)
    assert made is not stored and type(made) is Clock, "a transient answered with a stored instance"


def test_a_ready_singleton_detached_between_two_reads_never_answers_with_the_empty_marker():
    """Review round 7: the early return read the stored instance twice. When the host stopped
    (_detach_created_singletons) between the two reads, the caller got the library's empty
    marker instead of an object."""
    from py_autowired import ServiceRegistration

    container = Container()
    container.register_singleton(Clock)
    first = container.resolve(Clock)
    box = {"armed": True}

    class Racing(ServiceRegistration):
        @property
        def instance(self):
            value = self.__dict__["instance"]
            if box["armed"] and value is not container_module._UNSET:
                box["armed"] = False
                container._detach_created_singletons()  # the host stops right after this read
            return value

        @instance.setter
        def instance(self, value):
            self.__dict__["instance"] = value

    container.registrations[Clock].__class__ = Racing
    got = container.resolve(Clock)
    assert got is not container_module._UNSET, "the caller got the empty marker"
    assert isinstance(got, Clock)
    # E1b (gate): the early return answers with the object it read; the main path would read the
    # stored instance again, find it detached and build ANOTHER one.
    assert got is first, "a second object was built for a singleton that was ready"
