"""Regression: ready scoped services are read without locks (no new deadlocks)."""

import asyncio
import contextvars
import threading

import pytest

from py_autowired import (
    Container, DependencyInjectionError, LifetimeScope,
)


def test_scoped_factory_can_wait_for_another_thread_to_resolve_ready_service():
    container = Container()
    results, errors, workers = [], [], []

    class A:
        pass

    class B:
        pass

    def resolve_ready():
        try:
            results.append(container.resolve(A))
        except BaseException as exc:
            errors.append(exc)

    def factory(owner):
        context = contextvars.copy_context()
        worker = threading.Thread(target=context.run, args=(resolve_ready,), daemon=True)
        workers.append(worker)
        worker.start()
        worker.join(3)
        assert not worker.is_alive(), "ready scoped resolution blocked behind the factory"
        return B()

    container.register_scoped(A)
    container.register_factory(B, factory, LifetimeScope.SCOPED)
    try:
        with container.create_scope():
            ready = container.resolve(A)
            assert isinstance(container.resolve(B), B)
            assert results == [ready]
            assert errors == []
    finally:
        for worker in workers:
            worker.join(5)

