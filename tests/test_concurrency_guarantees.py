"""Concurrency guarantees added without new locks on hot paths:
1) a selection computed from a pre-refresh map is never cached after refresh();
2) closing a scope waits for an in-flight creation and disposes it;
3) a cross-thread singleton cycle raises CircularDependencyError instead of hanging."""

import threading
import types

import pytest

from py_autowired import CircularDependencyError, Container, LifetimeScope, autowired


def test_stale_selection_is_not_cached_after_refresh(monkeypatch):
    old = type("Clock", (), {"__module__": "feature.clock"})
    new = type("Clock", (), {"__module__": "feature.clock"})
    container = Container()
    container.register_transient(old)
    cache = autowired._CachedNameMap(container)
    original = autowired._pick_best_registration
    calls = []

    def racing_pick(regs, ctx):
        chosen = original(regs, ctx)
        if not calls:
            calls.append(1)
            # Another thread replaces the registration and refreshes while this
            # lookup still holds the old map's choice.
            container.registrations.pop(old)
            container.register_transient(new)
            cache.refresh()
        return chosen

    monkeypatch.setattr(autowired, "_pick_best_registration", racing_pick)
    first = cache.get_cached_registration("clock", "feature.consumer")
    assert first.service_type is old                     # stale answer returned once...
    again = cache.get_cached_registration("clock", "feature.consumer")
    assert again.service_type is new                     # ...but never cached


def test_scope_close_waits_for_in_flight_creation_and_disposes_it():
    started, release, closed = threading.Event(), threading.Event(), []

    class Resource:
        def close(self):
            closed.append(self)

    def factory(c):
        started.set()
        release.wait(5)
        return Resource()

    container = Container()
    container.register_factory(Resource, factory, LifetimeScope.SCOPED)
    scope = container.create_scope()
    created = []

    # Create inside the scope's state from a worker thread while main closes it.
    state = scope.state
    worker = threading.Thread(
        target=lambda: created.append(state.get_or_create(container.registrations[Resource], container)),
        daemon=True,
    )
    worker.start()
    assert started.wait(5)
    closer = threading.Thread(target=state.close, daemon=True)
    closer.start()
    closer.join(0.2)
    assert closer.is_alive()                             # close waits for the creation
    release.set()
    worker.join(5)
    closer.join(5)
    assert not closer.is_alive() and len(created) == 1
    assert closed == created                             # the in-flight instance is disposed


def test_cross_thread_singleton_cycle_raises_instead_of_deadlock():
    class A:
        pass

    class B:
        pass

    barrier = threading.Barrier(2, timeout=5)
    first = {"A": True, "B": True}

    def once(name):
        if first[name]:            # force the interleaving only on the first attempt
            first[name] = False
            barrier.wait()

    def make_a(c):
        once("A")
        c.resolve(B)
        return A()

    def make_b(c):
        once("B")
        c.resolve(A)
        return B()

    container = Container()
    container.register_factory(A, make_a, LifetimeScope.SINGLETON)
    container.register_factory(B, make_b, LifetimeScope.SINGLETON)
    errors = []

    def run(service):
        try:
            container.resolve(service)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,), daemon=True) for s in (A, B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "deadlock: threads still blocked"
    assert any(isinstance(e, CircularDependencyError) for e in errors)


def test_no_false_cycle_under_contention_on_an_acyclic_chain():
    import time

    names = [f"S{i}" for i in range(5)]
    classes = {n: type(n, (), {}) for n in names}
    container = Container()

    def factory_for(i):
        def factory(c):
            time.sleep(0.001)
            if i + 1 < len(names):
                c.resolve(classes[names[i + 1]])   # S0 -> S1 -> ... -> S4, no cycle
            return classes[names[i]]()
        return factory

    for i, n in enumerate(names):
        container.register_factory(classes[n], factory_for(i), LifetimeScope.SINGLETON)
    start, errors, results = threading.Barrier(16, timeout=5), [], []

    def run(i):
        start.wait()
        try:
            results.append(container.resolve(classes[names[i % len(names)]]))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads) and errors == [] and len(results) == 16
    for n in names:                                # still exactly one singleton each
        assert container.resolve(classes[n]) is container.resolve(classes[n])
