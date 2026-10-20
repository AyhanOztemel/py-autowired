"""Opt-in @Primary: among name-matched candidates a primary registration wins;
without any primary mark selection is exactly the previous behavior."""

import types

from py_autowired import Container, autowired


def _wire(monkeypatch, container, consumer):
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(consumer, container, cache.get_raw_map())
    autowired._patch_init(consumer, container, cache.get_raw_map())


def _graph():
    first = type("Clock", (), {"__module__": "feature.first"})
    second = type("Clock", (), {"__module__": "feature.other"})
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "clock_instance": None})
    container = Container()
    container.register_transient(first)
    container.register_transient(second)
    return container, first, second, consumer


def test_without_primary_selection_is_unchanged(monkeypatch):
    container, first, second, consumer = _graph()
    _wire(monkeypatch, container, consumer)
    assert type(consumer().clock_instance) is first          # registration order


def test_primary_registration_wins(monkeypatch):
    container, first, second, consumer = _graph()
    container.set_primary(second)
    _wire(monkeypatch, container, consumer)
    assert type(consumer().clock_instance) is second
