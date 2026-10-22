"""0.5.4: the active name map follows registration changes made after autowiring started.
Before, a patched class kept getting the OLD implementation after replace() until refresh() was
called, while resolve() already gave the new one (silent, since 0.5.3)."""

import importlib
import sys
import uuid

import pytest

from py_autowired import Container, activate_container, auto_inject, autowired


class Service:
    pass


class OldImpl(Service):
    pass


class NewImpl(Service):
    pass


class Extra:
    pass


@pytest.fixture
def consumer_module(tmp_path, monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"proj_{tag}"
    root.mkdir()
    (root / f"consumer_{tag}.py").write_text(
        "class Consumer:\n"
        "    def __init__(self):\n"
        "        self.service_instance = None\n"
        "        self.extra_instance = None\n"
        "        self.helper_instance = None\n")
    sys.path.insert(0, str(root))
    try:
        container = Container()
        container.register_singleton(Service, OldImpl)
        auto_inject(container, root_dir=str(root))
        activate_container(container)
        yield container, importlib.import_module(f"consumer_{tag}")
    finally:
        sys.path.remove(str(root))
        sys.modules.pop(f"consumer_{tag}", None)


def test_a_replaced_service_reaches_new_consumers_without_refresh(consumer_module):
    container, module = consumer_module
    assert type(module.Consumer().service_instance) is OldImpl
    container.replace_singleton(Service, NewImpl)
    assert type(container.resolve(Service)) is NewImpl
    assert type(module.Consumer().service_instance) is NewImpl, "the field kept the replaced implementation"


def test_a_replace_before_any_consumer_is_followed_too(consumer_module):
    container, module = consumer_module
    container.replace_singleton(Service, NewImpl)
    assert type(module.Consumer().service_instance) is NewImpl


def test_a_registration_added_later_is_found(consumer_module):
    container, module = consumer_module
    assert module.Consumer().extra_instance is None
    container.register_singleton(Extra)
    assert type(module.Consumer().extra_instance) is Extra, "a later registration was not found"


def test_an_alias_added_later_is_found(consumer_module):
    container, module = consumer_module

    class Helper:
        pass

    assert module.Consumer().helper_instance is None
    container.register_alias(Helper, Service)
    assert type(module.Consumer().helper_instance) is OldImpl, "a later alias was not found"


def test_an_unchanged_container_does_not_rebuild_the_map(consumer_module, monkeypatch):
    container, module = consumer_module
    module.Consumer()
    builds = []
    real = autowired._build_name_map_from_container
    monkeypatch.setattr(autowired, "_build_name_map_from_container",
                        lambda c: builds.append(1) or real(c))
    for _ in range(5):
        module.Consumer()
    assert builds == [], "the name map was rebuilt without a registration change"
    container.replace_singleton(Service, NewImpl)
    module.Consumer()
    module.Consumer()
    assert builds == [1], f"one change should rebuild once, got {len(builds)}"


def test_a_skipped_fallback_is_not_a_change(consumer_module):
    container, _module = consumer_module
    before = container._changes
    container.register_singleton_fallback(Service, NewImpl)   # already registered: skipped
    assert container._changes == before


def test_a_fallback_skipped_under_the_lock_is_not_a_change(consumer_module, monkeypatch):
    """The race path: another thread registered the type between the quick check and the lock."""
    container, _module = consumer_module
    monkeypatch.setattr(container, "is_registered", lambda service_type: False)
    before = container._changes
    assert container.register_singleton_fallback(Service, NewImpl) is False
    assert container._changes == before, "a skipped fallback counted as a change"


def test_a_change_during_the_rebuild_is_not_lost(consumer_module, monkeypatch):
    container, module = consumer_module
    real = autowired._build_name_map_from_container
    once = []

    def build(c):
        built = real(c)
        if not once:
            once.append(1)
            c.register_singleton(Extra)       # another thread registers after the snapshot
        return built

    monkeypatch.setattr(autowired, "_build_name_map_from_container", build)
    container.replace_singleton(Service, NewImpl)   # the next lookup rebuilds
    consumer = module.Consumer()
    assert type(consumer.service_instance) is NewImpl
    assert type(consumer.extra_instance) is Extra, "a registration made during the rebuild was lost"


def _plain_cache():
    import threading as _t  # noqa: F401
    container = Container()
    container.register_singleton(Service, OldImpl)
    activate_container(container)
    cache = autowired._NAME_MAP_CACHE
    cache.get_raw_map()
    return container, cache, autowired._field_key("service_instance")


def test_an_old_choice_stored_during_a_rebuild_never_stays(monkeypatch):
    """A lookup chose from the old map; a rebuild cleared the choices; the lookup then stored its
    old choice. It must not stay cached (it would hand out the replaced implementation forever)."""
    import threading

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    container, cache, key = _plain_cache()
    computed, go_a, a_done = threading.Event(), threading.Event(), threading.Event()
    real_pick = autowired._pick_best_registration

    def pick(regs, ctx):
        chosen = real_pick(regs, ctx)
        if threading.current_thread().name == "A" and not computed.is_set():
            computed.set()
            go_a.wait(5)                                   # A chose from the old map and waits
        return chosen

    class HookDict(dict):
        def clear(self):
            super().clear()
            if threading.current_thread().name == "B":
                go_a.set()                                 # B cleared the choices: A stores now
                a_done.wait(5)

    monkeypatch.setattr(autowired, "_pick_best_registration", pick)
    cache._resolution_cache = HookDict()

    def thread_a():
        cache.get_cached_registration(key, "app.services")
        a_done.set()

    a = threading.Thread(target=thread_a, name="A")
    a.start()
    assert computed.wait(5)
    container.replace_singleton(Service, NewImpl)
    b = threading.Thread(target=lambda: cache.get_cached_registration(key, "app.services"), name="B")
    b.start()
    a.join(5)
    b.join(5)
    monkeypatch.setattr(autowired, "_pick_best_registration", real_pick)
    later = cache.get_cached_registration(key, "app.services")
    assert later.implementation_type is NewImpl, "a choice from the replaced map stayed cached"


def test_a_cached_choice_is_read_once(monkeypatch):
    """A rebuild clearing the choices between "is it cached?" and "read it" must not raise."""
    import threading

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    container, cache, key = _plain_cache()
    cache.get_cached_registration(key, "app.x")            # the choice is cached
    looked, cleared = threading.Event(), threading.Event()

    class HookDict(dict):
        def __contains__(self, k):
            found = super().__contains__(k)
            if threading.current_thread().name == "A" and found and not looked.is_set():
                looked.set()
                cleared.wait(5)                            # A: "it is there", not read yet
            return found

        def get(self, k, default=None):
            value = super().get(k, default)
            if threading.current_thread().name == "A" and not looked.is_set():
                looked.set()
                cleared.wait(5)                            # A has read it; a clear now is harmless
            return value

        def clear(self):
            super().clear()
            cleared.set()

    cache._resolution_cache = HookDict(cache._resolution_cache)
    out = {}

    def thread_a():
        try:
            out["A"] = cache.get_cached_registration(key, "app.x")
        except Exception as error:  # noqa: BLE001 - the test inspects it
            out["A"] = error

    a = threading.Thread(target=thread_a, name="A")
    a.start()
    assert looked.wait(5)
    container.replace_singleton(Service, NewImpl)
    cache.get_raw_map()                                    # another request rebuilds
    a.join(5)
    assert not isinstance(out["A"], Exception), f"a lookup raised: {out['A']!r}"


def test_a_name_with_no_registration_is_cached_too(monkeypatch):
    """"No registration" (None) is a cached answer: the next lookup of that name is a hit."""
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    _container, cache, _key = _plain_cache()
    missing = autowired._field_key("nothing_registered_instance")
    assert cache.get_cached_registration(missing, "app.x") is None
    calls = []
    real = autowired._CachedNameMap.get_raw_map
    monkeypatch.setattr(autowired._CachedNameMap, "get_raw_map",
                        lambda self: calls.append(1) or real(self))
    assert cache.get_cached_registration(missing, "app.x") is None
    assert calls == [], "a cached 'no registration' answer was computed again"
