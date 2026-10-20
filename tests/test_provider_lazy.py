"""Opt-in deferred resolution: Provider[T] resolves on every call (respecting the
registered lifetime and the scope active at call time); Lazy[T] resolves once on
first .value access. Constructor parameters annotated with them are filled."""

import pytest

from py_autowired import Container, Lazy, Provider, ScopeNotActiveError


class Session:
    pass


class Clock:
    pass


class Service:
    def __init__(self, sessions: Provider[Session], clock: Lazy[Clock]):
        self.sessions = sessions
        self.clock = clock


def test_provider_follows_the_scope_active_at_call_time():
    container = Container()
    container.register_scoped(Session)
    container.register_singleton(Clock)
    container.register_singleton(Service)
    service = container.resolve(Service)          # singleton, no scope needed
    with container.create_scope():
        a1, a2 = service.sessions(), service.sessions()
    with container.create_scope():
        b = service.sessions()
    assert a1 is a2 and b is not a1                # scoped per scope, no capture
    with pytest.raises(ScopeNotActiveError):
        service.sessions()


def test_lazy_resolves_once_on_first_access():
    created = []

    class Heavy:
        def __init__(self):
            created.append(self)

    class User:
        def __init__(self, heavy: Lazy[Heavy]):
            self.heavy = heavy

    container = Container()
    container.register_transient(Heavy)
    container.register_transient(User)
    user = container.resolve(User)
    assert created == []                            # nothing built yet
    first = user.heavy.value
    assert user.heavy.value is first and created == [first]


def test_provider_and_lazy_can_be_built_directly():
    container = Container()
    container.register_transient(Clock)
    assert isinstance(Provider(container, Clock)(), Clock)
    assert isinstance(Lazy(container, Clock).value, Clock)


def test_validation_accepts_provider_of_scoped_in_singleton(monkeypatch):
    import types
    from py_autowired import autowired, validate_autowiring

    module = types.ModuleType("provider_probe")
    exec(
        "from py_autowired import Provider\n"
        "class Session: pass\n"
        "class Holder:\n"
        "    def __init__(self, sessions: Provider[Session]):\n"
        "        self.sessions = sessions\n"
        "class Consumer:\n"
        "    holder_instance = None\n",
        module.__dict__,
    )
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)
    container = Container()
    container.register_scoped(module.Session)
    container.register_singleton(module.Holder)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(module.Consumer, container, cache.get_raw_map())
    autowired._patch_init(module.Consumer, container, cache.get_raw_map())
    validate_autowiring(container)   # no capture, no "unregistered" false alarm
