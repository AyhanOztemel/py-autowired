"""Fourth wave of mutation-driven tests (round 5 survivors)."""

import pytest

from py_autowired import AutoInjectionResolutionError, Container, autowired


def _patch(container, *classes, cache=True, monkeypatch=None):
    name_map = autowired._build_name_map_from_container(container)
    if monkeypatch is not None:
        monkeypatch.setattr(autowired, "_NAME_MAP_CACHE",
                            autowired._CachedNameMap(container) if cache else None)
    for cls in classes:
        autowired._patch_setattr(cls, container, name_map)
        autowired._patch_init(cls, container, name_map)


def test_field_read_failure_does_not_stop_later_fields(monkeypatch):
    service = type("Zeta", (), {"__module__": "feature.services"})

    class Consumer:
        __module__ = "feature.consumer"
        zeta_instance = None

        @property
        def alpha_instance(self):          # sorted first; reading it fails
            raise RuntimeError("boom")

        @alpha_instance.setter
        def alpha_instance(self, value):
            pass

    container = Container()
    container.register_transient(service)
    _patch(container, Consumer, monkeypatch=monkeypatch)
    assert isinstance(Consumer().zeta_instance, service)


def test_eager_setattr_resolution_failure_is_wrapped(monkeypatch):
    class Broken:
        def __init__(self, missing: "NotRegistered"):
            pass

    Broken.__module__ = "feature.services"
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "broken_instance": None})
    container = Container()
    container.register_transient(Broken)
    _patch(container, consumer, monkeypatch=monkeypatch)
    obj = object.__new__(consumer)
    with pytest.raises(AutoInjectionResolutionError, match="broken_instance"):
        obj.broken_instance = None              # eager injection through __setattr__


def test_without_active_cache_the_patch_time_name_map_is_used(monkeypatch):
    service = type("Clock", (), {"__module__": "feature.clock"})
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "clock_instance": None})
    container = Container()
    container.register_transient(service)
    _patch(container, consumer, cache=False, monkeypatch=monkeypatch)
    assert autowired._NAME_MAP_CACHE is None
    assert isinstance(consumer().clock_instance, service)


def test_patching_twice_does_not_wrap_twice(monkeypatch):
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "clock_instance": None})
    container = Container()
    _patch(container, consumer, monkeypatch=monkeypatch)
    setattr_once, init_once = consumer.__setattr__, consumer.__init__
    _patch(container, consumer, monkeypatch=monkeypatch)
    assert consumer.__setattr__ is setattr_once and consumer.__init__ is init_once


def test_bookkeeping_failure_never_breaks_patching(monkeypatch):
    service = type("Clock", (), {"__module__": "feature.clock"})
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "clock_instance": None})
    container = Container()
    container.register_transient(service)

    def explode(*_):
        raise RuntimeError("bookkeeping failed")

    monkeypatch.setattr(autowired, "_remember_patched_class", explode)
    _patch(container, consumer, monkeypatch=monkeypatch)
    assert isinstance(consumer().clock_instance, service)


class NoWeakref:
    """A duck-typed container that does not support weak references."""

    __slots__ = ("_inner",)

    def __init__(self):
        object.__setattr__(self, "_inner", Container())

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_non_weakrefable_container_is_supported(monkeypatch):
    duck = NoWeakref()
    service = type("Clock", (), {"__module__": "feature.clock"})
    duck.register_transient(service)
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "clock_instance": None})
    autowired._remember_patched_class(duck, consumer)          # must not raise
    assert consumer in autowired._PATCHED_CLASSES[id(duck)].values()
