"""0.5.4 review round 19 (speed): injected() checks a class's fields once it knows the class is
patched (a patched class stays patched), and every time for a class that is not - the checks of an
unpatched class are never skipped."""

import types

import pytest

pytest.importorskip("fastapi")

from py_autowired import Container, activate_container, autowired  # noqa: E402
from py_autowired import fastapi as bridge  # noqa: E402


class _Repo:
    pass


def _call(dependency, container):
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(
        py_autowired_container=container)))
    coroutine = dependency(request)
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("the dependency awaited something")


@pytest.fixture
def counted(monkeypatch):
    calls = []
    real_registered, real_built = bridge._warn_unfilled_registered, bridge._inject_unpatched
    monkeypatch.setattr(bridge, "_warn_unfilled_registered",
                        lambda instance: calls.append("registered") or real_registered(instance))
    monkeypatch.setattr(bridge, "_inject_unpatched",
                        lambda instance, container: calls.append("built") or real_built(instance, container))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    return calls


def _container(*registered):
    container = Container()
    container.register_singleton(_Repo)
    for cls in registered:
        container.register_transient(cls)
    activate_container(container)
    return container


def test_a_patched_class_is_checked_once_per_dependency(counted):
    class Registered:
        def __init__(self):
            self.repo_instance = None

    class Built:
        def __init__(self):
            self.repo_instance = None

    container = _container(Registered)
    name_map = autowired._NAME_MAP_CACHE.get_raw_map()
    for cls in (Registered, Built):
        autowired._patch_setattr(cls, container, name_map)
        autowired._patch_init(cls, container, name_map)
    registered, built = bridge.injected(Registered), bridge.injected(Built)
    for _ in range(3):
        assert isinstance(_call(registered, container).repo_instance, _Repo)
        assert isinstance(_call(built, container).repo_instance, _Repo)
    assert counted.count("registered") == 1, counted
    assert counted.count("built") == 1, counted


def test_an_unpatched_class_is_checked_every_time(counted):
    class Registered:
        def __init__(self):
            self.repo_instance = None

    class Built:
        def __init__(self):
            self.repo_instance = None

    container = _container(Registered)
    registered, built = bridge.injected(Registered), bridge.injected(Built)
    for _ in range(3):
        _call(registered, container)
        assert isinstance(_call(built, container).repo_instance, _Repo)
    assert counted.count("registered") == 3, counted
    assert counted.count("built") == 3, counted
