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
    """Stands in for the thread-local resolution depth and records every write."""

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
    monkeypatch.setattr(container_module, "_RESOLVING", depth)
    for _ in range(3):
        assert container.resolve(Clock) is first
    assert flush.calls == 0, "a ready singleton flushed the deferred warnings"
    assert depth.writes == [], "a ready singleton changed the resolution depth"


def test_a_supplied_instance_is_ready_too(monkeypatch):
    container = Container()
    clock = Clock()
    container.register_instance(Clock, clock)
    flush = Counting(container_module._flush_warnings)
    monkeypatch.setattr(container_module, "_flush_warnings", flush)
    assert container.resolve(Clock) is clock
    assert flush.calls == 0


def test_a_singleton_not_created_yet_still_does_the_bookkeeping(monkeypatch):
    container = Container()
    container.register_singleton(Clock)
    flush = Counting(container_module._flush_warnings)
    monkeypatch.setattr(container_module, "_flush_warnings", flush)
    container.resolve(Clock)
    assert flush.calls == 1


@pytest.mark.parametrize("scope", [LifetimeScope.TRANSIENT, LifetimeScope.SCOPED])
def test_other_lifetimes_still_do_the_bookkeeping(monkeypatch, scope):
    container = Container()
    container.register(Clock, Clock, scope)
    flush = Counting(container_module._flush_warnings)
    monkeypatch.setattr(container_module, "_flush_warnings", flush)
    with container.create_scope():
        container.resolve(Clock)
        container.resolve(Clock)
    assert flush.calls == 2


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
