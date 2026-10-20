"""Opt-in keyed services (.NET keyed services / Spring @Qualifier for one type).
Keyed registrations live outside the name map, so runtime field injection is
unchanged; constructor parameters use Annotated[T, Keyed(key)]."""

import types
from typing import Annotated

import pytest

from py_autowired import (
    CircularDependencyError, Container, Keyed, ServiceNotRegisteredError, autowired,
    validate_autowiring,
)


class IClock:
    pass


class UtcClock(IClock):
    pass


class LocalClock(IClock):
    pass


def test_keyed_registrations_of_one_type_resolve_separately():
    container = Container()
    container.register_keyed(IClock, "utc", UtcClock)
    container.register_keyed(IClock, "local", LocalClock)
    assert type(container.resolve_keyed(IClock, "utc")) is UtcClock
    assert type(container.resolve_keyed(IClock, "local")) is LocalClock
    with pytest.raises(ServiceNotRegisteredError):
        container.resolve_keyed(IClock, "mars")
    with pytest.raises(ServiceNotRegisteredError):
        container.resolve(IClock)                     # keyed != unkeyed


def test_keyed_lifetimes_are_per_key_and_separate_from_unkeyed():
    container = Container()
    container.register_scoped(IClock, UtcClock)
    container.register_keyed(IClock, "a", UtcClock, scope=container_scope("SCOPED"))
    container.register_keyed(IClock, "b", UtcClock, scope=container_scope("SCOPED"))
    container.register_keyed(IClock, "s", UtcClock, scope=container_scope("SINGLETON"))
    with container.create_scope():
        plain, a, b = (container.resolve(IClock), container.resolve_keyed(IClock, "a"),
                       container.resolve_keyed(IClock, "b"))
        assert len({id(plain), id(a), id(b)}) == 3
        assert container.resolve_keyed(IClock, "a") is a
    assert container.resolve_keyed(IClock, "s") is container.resolve_keyed(IClock, "s")


def container_scope(name):
    from py_autowired import LifetimeScope
    return getattr(LifetimeScope, name)


def test_constructor_parameter_uses_keyed_registration():
    class Report:
        def __init__(self, clock: Annotated[IClock, Keyed("utc")]):
            self.clock = clock

    container = Container()
    container.register_keyed(IClock, "utc", UtcClock)
    container.register_keyed(IClock, "local", LocalClock)
    container.register_transient(Report)
    assert type(container.resolve(Report).clock) is UtcClock


class Node:
    def __init__(self, other: Annotated["Node", Keyed("next")]):
        self.other = other


class Chain:
    def __init__(self, tail: Annotated[IClock, Keyed("utc")]):
        self.tail = tail


def test_keyed_cycle_is_reported_and_other_keys_are_not_a_cycle():
    container = Container()
    container.register_keyed(Node, "next", Node)
    with pytest.raises(CircularDependencyError, match=r"Node\['next'\]"):
        container.resolve_keyed(Node, "next")
    # same type under different keys is not a cycle
    container.register_keyed(IClock, "utc", UtcClock)
    container.register_keyed(Chain, "a", Chain)
    assert type(container.resolve_keyed(Chain, "a").tail) is UtcClock


def test_keyed_registrations_are_invisible_to_field_injection(monkeypatch):
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "utc_clock_instance": None})
    container = Container()
    container.register_keyed(IClock, "utc", UtcClock)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(consumer, container, cache.get_raw_map())
    autowired._patch_init(consumer, container, cache.get_raw_map())
    assert consumer().utc_clock_instance is None


def test_validation_accepts_keyed_constructor_parameter(monkeypatch):
    module = types.ModuleType("keyed_probe")
    exec(
        "from typing import Annotated\n"
        "from py_autowired import Keyed\n"
        "class IClock: pass\n"
        "class UtcClock(IClock): pass\n"
        "class Report:\n"
        "    def __init__(self, clock: Annotated[IClock, Keyed('utc')]):\n"
        "        self.clock = clock\n"
        "class Consumer:\n"
        "    report_instance = None\n",
        module.__dict__,
    )
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)
    container = Container()
    container.register_keyed(module.IClock, "utc", module.UtcClock)
    container.register_transient(module.Report)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(module.Consumer, container, cache.get_raw_map())
    autowired._patch_init(module.Consumer, container, cache.get_raw_map())
    validate_autowiring(container)
    assert type(module.Consumer().report_instance.clock) is module.UtcClock
