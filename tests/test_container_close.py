"""Opt-in shutdown: Container.close()/aclose() dispose container-created
singletons in reverse creation order (.NET semantics: caller-supplied
register_instance objects are owned by the caller and are not disposed)."""

import asyncio

import pytest

from py_autowired import AsyncDisposalRequiredError, Container, ContainerClosedError


def _resource(log, name, fail=None, async_close=False):
    if async_close:
        async def aclose(self):
            log.append(name)
            if fail:
                raise fail
        return type(name, (), {"aclose": aclose})

    def close(self):
        log.append(name)
        if fail:
            raise fail
    return type(name, (), {"close": close})


def test_close_disposes_singletons_in_reverse_creation_order():
    log = []
    A, B, C = (_resource(log, n) for n in "ABC")
    container = Container()
    for cls in (A, B, C):
        container.register_singleton(cls)
    container.resolve(B)
    container.resolve(A)
    container.resolve(C)
    container.close()
    assert log == ["C", "A", "B"]


def test_unresolved_and_supplied_instances_are_not_disposed():
    log = []
    A, Supplied = _resource(log, "A"), _resource(log, "Supplied")
    container = Container()
    container.register_singleton(A)              # never resolved
    container.register_instance(Supplied, Supplied())
    container.resolve(Supplied)
    container.close()
    assert log == []


def test_close_is_idempotent_and_resolve_after_close_fails():
    log = []
    A = _resource(log, "A")
    container = Container()
    container.register_singleton(A)
    container.resolve(A)
    container.close()
    container.close()
    assert log == ["A"]
    with pytest.raises(ContainerClosedError):
        container.resolve(A)


def test_close_keeps_first_error_and_disposes_everything():
    log = []
    first, second = ValueError("first"), LookupError("second")
    A = _resource(log, "A", fail=second)
    B = _resource(log, "B", fail=first)
    container = Container()
    container.register_singleton(A)
    container.register_singleton(B)
    container.resolve(A)
    container.resolve(B)
    with pytest.raises(ValueError) as error:
        container.close()
    assert error.value is first and log == ["B", "A"]
    assert any("second" in note for note in getattr(first, "__notes__", ()))


def test_sync_close_rejects_async_only_resource_and_aclose_handles_it():
    log = []
    Async = _resource(log, "Async", async_close=True)
    sync_container = Container()
    sync_container.register_singleton(Async)
    sync_container.resolve(Async)
    with pytest.raises(AsyncDisposalRequiredError):
        sync_container.close()

    async_container = Container()
    async_container.register_singleton(Async)
    async_container.resolve(Async)
    asyncio.run(async_container.aclose())
    assert log == ["Async"]
