"""Mutation-driven tests for opt-in startup validation (round 4 survivors)."""

import logging
from typing import Annotated

import pytest

from py_autowired import (
    AutowiringValidationError, Container, Keyed, Provider, factory_dependencies,
    validate_autowiring,
)
from py_autowired import autowired


@pytest.fixture
def graph(monkeypatch):
    container = Container()
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._CachedNameMap(container))

    def patch(*classes):
        name_map = autowired._build_name_map_from_container(container)
        for cls in classes:
            autowired._patch_setattr(cls, container, name_map)
            autowired._patch_init(cls, container, name_map)
        return container

    return container, patch


def _issues(container):
    try:
        validate_autowiring(container)
    except AutowiringValidationError as exc:
        return list(exc.issues)
    return []


class IClock:
    pass


class Missing:
    pass


class Session:
    pass


def test_keyed_parameters_are_checked_one_by_one(graph):
    container, patch = graph

    class KeyedService:
        def __init__(self, a: Annotated[IClock, Keyed("a")], b: Annotated[IClock, Keyed("b")],
                     c: Annotated[IClock, Keyed("c")] = None):
            pass

    consumer = type("Consumer", (), {"__module__": "app.c", "keyed_service_instance": None})
    container.register_transient(KeyedService)
    patch(consumer)
    issues = _issues(container)
    text = "\n".join(issues)
    assert "KeyedService.a requires unregistered keyed" in text
    assert "KeyedService.b requires unregistered keyed" in text      # loop continues
    assert "KeyedService.c" not in text                               # default is fine


def test_deferred_unregistered_is_reported_unless_instance_exists(graph):
    container, patch = graph

    class Holder:
        def __init__(self, missing: Provider[Missing], next_: Provider[Missing]):
            pass

    consumer = type("Consumer", (), {"__module__": "app.c", "holder_instance": None})
    container.register_transient(Holder)
    patch(consumer)
    text = "\n".join(_issues(container))
    assert "Holder.missing defers an unregistered dependency" in text
    assert "Holder.next_ defers an unregistered dependency" in text

    supplied = Container()
    supplied.register_instance(Holder, object.__new__(Holder))       # instance exists: lifetime-only
    consumer2 = type("Consumer2", (), {"__module__": "app.c", "holder_instance": None})
    name_map = autowired._build_name_map_from_container(supplied)
    autowired._patch_setattr(consumer2, supplied, name_map)
    autowired._patch_init(consumer2, supplied, name_map)
    assert _issues(supplied) == []


def test_supplied_singleton_instance_skips_constructor_checks(graph):
    container, patch = graph

    class NeedsMissing:
        def __init__(self, dep: Missing):
            pass

    consumer = type("Consumer", (), {"__module__": "app.c", "needs_missing_instance": None})
    container.register_instance(NeedsMissing, object.__new__(NeedsMissing))
    patch(consumer)
    assert _issues(container) == []
    container2 = Container()
    container2.register_transient(NeedsMissing)
    consumer2 = type("Consumer2", (), {"__module__": "app.c", "needs_missing_instance": None})
    name_map = autowired._build_name_map_from_container(container2)
    autowired._patch_setattr(consumer2, container2, name_map)
    autowired._patch_init(consumer2, container2, name_map)
    assert any("NeedsMissing.dep requires an unregistered dependency" in i for i in _issues(container2))


def test_factory_metadata_unregistered_dependency(graph):
    container, patch = graph

    @factory_dependencies(Missing)
    def build(c):
        return object()

    container.register_factory(IClock, build)
    consumer = type("Consumer", (), {"__module__": "app.c", "clock_instance": None})
    patch(consumer)
    assert any("factory requires unregistered" in i for i in _issues(container))


def test_singleton_consumer_capturing_scoped_is_reported_only_for_registered_singletons(graph):
    container, patch = graph
    container.register_scoped(Session)
    registered = type("Registered", (), {"__module__": "app.c", "session_instance": None})
    unregistered = type("Unregistered", (), {"__module__": "app.c", "session_instance": None})
    container.register_singleton(registered)
    patch(registered, unregistered)
    issues = _issues(container)
    assert any("Registered.session_instance" in i and "captures scoped" in i for i in issues)
    assert not any("Unregistered.session_instance" in i for i in issues)


def test_single_candidate_is_not_ambiguous(graph):
    container, patch = graph
    clock = type("Clock", (), {"__module__": "app.clock"})
    container.register_transient(clock)
    consumer = type("Consumer", (), {"__module__": "app.c", "clock_instance": None})
    patch(consumer)
    assert _issues(container) == []


def test_unpatched_registered_class_fields_are_not_followed(graph):
    container, patch = graph

    class Broken:
        def __init__(self, dep: Missing):
            pass

    class Plain:                       # registered but never patched by autowiring
        broken_instance = None

    container.register_transient(Broken)
    container.register_transient(Plain)
    consumer = type("Consumer", (), {"__module__": "app.c", "plain_instance": None})
    patch(consumer)
    assert _issues(container) == []


def test_inherited_field_warning_rules(graph, caplog, monkeypatch):
    container, patch = graph
    # Base stands for a PROJECT class (its module "app.c" is not loaded): a base outside the
    # project is read like the class itself since 0.5.4 (tests/test_base_fields.py).
    monkeypatch.setattr(autowired, "_in_project", lambda cls: True)
    base = type("Base", (), {"__module__": "app.c", "x_instance": None,
                             "y_instance": "set", "helper": None})
    child = type("Child", (base,), {"__module__": "app.c"})
    own = type("Own", (base,), {"__module__": "app.c", "x_instance": None})
    patch(child, own)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        _issues(container)
    text = caplog.text
    assert "Child.x_instance: inherited unannotated field" in text
    assert "y_instance" not in text and "helper" not in text          # non-None / not a field
    assert "Own.x_instance: inherited" not in text                     # redefined in the subclass
    assert "Base.x_instance: inherited" not in text                    # a class does not inherit from itself
