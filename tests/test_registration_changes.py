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
                        lambda c, **kw: builds.append(1) or real(c, **kw))
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

    def build(c, **kw):
        built = real(c, **kw)
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

    real_build = autowired._build_name_map_from_container

    def build(c, **kw):
        built = real_build(c, **kw)
        if threading.current_thread().name == "B":
            go_a.set()                                     # B is rebuilding: A stores its old choice now
            a_done.wait(5)
        return built

    monkeypatch.setattr(autowired, "_pick_best_registration", pick)
    monkeypatch.setattr(autowired, "_build_name_map_from_container", build)

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

    cache._resolution_cache = HookDict(cache._resolution_cache)
    real_build = autowired._build_name_map_from_container

    def build(c, **kw):
        built = real_build(c, **kw)
        cleared.set()                                      # a rebuild drops the cached choices
        return built
    monkeypatch.setattr(autowired, "_build_name_map_from_container", build)
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


def test_an_implementation_found_late_by_resolve_is_a_change(monkeypatch):
    """resolve() completes an interface-only registration in place: the name map must follow."""
    import abc

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)

    class ILateRepoR13(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container = Container()
    container.register_singleton(ILateRepoR13)              # its implementation is not imported yet
    activate_container(container)
    cache = autowired._NAME_MAP_CACHE
    cache.get_raw_map()

    class SqlLateRepoR13(ILateRepoR13):
        def run(self):
            return "sql"

    before = container._changes
    assert type(container.resolve(ILateRepoR13)) is SqlLateRepoR13
    assert container._changes > before, "completing the implementation in place was not a change"
    found = cache.get_cached_registration(autowired._field_key("sqllaterepor13_instance"), "app.x")
    assert found is not None and found.implementation_type is SqlLateRepoR13


def test_an_implementation_completed_at_startup_is_a_change(monkeypatch):
    import abc

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)

    class IStartRepoR13(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container = Container()
    container.register_singleton(IStartRepoR13)
    activate_container(container)
    autowired._NAME_MAP_CACHE.get_raw_map()

    class MemStartRepoR13(IStartRepoR13):
        def run(self):
            return "mem"

    before = container._changes
    container._complete_implementations()
    assert container._changes > before, "completing implementations at startup was not a change"


def test_the_name_map_log_is_written_outside_the_cache_lock(consumer_module, monkeypatch):
    """A log handler that builds an injected object while "Name map built" is logged must not
    wait for the lock its own thread holds (it would hang forever)."""
    import logging
    import threading

    container, module = consumer_module
    monkeypatch.setattr(autowired, "DEBUG_MODE", True)
    inside = []

    class Building(logging.Handler):
        def handle(self, record):                            # no handler lock: a hung emit must not
            self.emit(record)                                # block logging's shutdown at exit
            return True

        def emit(self, record):
            if "Name map built" in record.getMessage() and not inside:
                inside.append(1)
                module.Consumer()                            # field injection inside the handler

    handler = Building(level=logging.DEBUG)
    log = logging.getLogger("py_autowired")
    old_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        container.replace_singleton(Service, NewImpl)        # the next lookup rebuilds the map
        worker = threading.Thread(target=module.Consumer, daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "the rebuild hung on its own lock (log written under the lock)"
        assert inside, "the handler never ran"
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)



def test_an_old_choice_stored_after_a_rebuild_is_never_seen_by_another_lookup(monkeypatch):
    """T1 chose from the old map and stores its choice only after T2's replace() and rebuild; it is
    paused right after storing. A lookup made in that moment must get the new implementation."""
    import threading

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    container, cache, key = _plain_cache()
    chosen, stored, go_store, release = (threading.Event() for _ in range(4))
    real_pick = autowired._pick_best_registration

    def pick(regs, ctx):
        choice = real_pick(regs, ctx)
        if threading.current_thread().name == "T1":
            chosen.set()
            go_store.wait(5)                               # T1 chose from the old map, waits to store
        return choice

    class Pausing(dict):
        def setdefault(self, k, default=None):
            value = super().setdefault(k, default)
            if threading.current_thread().name == "T1":
                stored.set()
                release.wait(5)                            # stored, not yet checked
            return value

    monkeypatch.setattr(autowired, "_pick_best_registration", pick)
    cache._resolution_cache = Pausing()
    t1 = threading.Thread(target=lambda: cache.get_cached_registration(key, "app.services"), name="T1")
    t1.start()
    assert chosen.wait(5)
    container.replace_singleton(Service, NewImpl)
    other = autowired._field_key("extra_instance")
    cache.get_cached_registration(other, "app.services")    # T2: another field: rebuilds the map
    go_store.set()
    assert stored.wait(5)
    seen = cache.get_cached_registration(key, "app.services")  # a lookup while T1 sits after storing
    release.set()
    t1.join(5)
    assert seen.implementation_type is NewImpl, "a lookup got the replaced implementation"


def test_a_lookup_during_refresh_never_leaves_an_old_choice_behind(monkeypatch):
    """R5d: a registered class is renamed (its __name__ changes; the change count does not), then
    refresh() runs. A lookup made while refresh() builds the new map chooses from the old map; that
    choice must not be what lookups get after the refresh."""
    import threading

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    clock = type("OldClock", (), {})
    container = Container()
    container.register_singleton(clock)
    activate_container(container)
    cache = autowired._NAME_MAP_CACHE
    ctx = "app.services"
    assert cache.get_cached_registration("oldclock", ctx) is not None
    registration = container.registrations[clock]
    clock.__name__ = "NewClock"                           # the map changes; the counter does not
    before = container._changes
    building, release = threading.Event(), threading.Event()
    real_build = autowired._build_name_map_from_container

    def build(c, **kw):
        built = real_build(c, **kw)
        if threading.current_thread().name == "refresh":
            building.set()
            release.wait(5)                               # refresh() holds the new map, not yet installed
        return built
    monkeypatch.setattr(autowired, "_build_name_map_from_container", build)
    cache._resolution_cache.pop(("oldclock", ctx), None)  # the next lookup of the old name is a miss
    refresher = threading.Thread(target=cache.refresh, name="refresh")
    refresher.start()
    try:
        assert building.wait(5), "refresh() never reached the map build"
        during = cache.get_cached_registration("oldclock", ctx)  # a miss inside the window
    finally:
        release.set()
        refresher.join(5)
    assert not refresher.is_alive()
    assert container._changes == before
    assert during is registration                          # read from the old map inside the window
    assert cache.get_cached_registration("oldclock", ctx) is None, "an old choice outlived the refresh"
    assert cache.get_cached_registration("newclock", ctx) is registration


def _rebuild_with_a_finalizer(monkeypatch, on_del, trigger):
    """The whole story runs in ONE thread, so the replaced registration is freed where the rebuild
    drops it (a free-threaded build frees an object another thread drops later, in its owner).
    Returns (finalized, done, still running)."""
    import threading

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    finalized, done = threading.Event(), threading.Event()

    class Factory:  # the last reference to it is the replaced registration, kept by the old cache
        def __call__(self, c):
            return OldImpl()

        def __del__(self):
            finalized.set()
            on_del()
            done.set()

    def story():
        container = Container()
        container.register_factory(Service, Factory())
        activate_container(container)
        cache = autowired._NAME_MAP_CACHE
        key = autowired._field_key("service_instance")
        assert cache.get_cached_registration(key, "app").factory is not None  # the old choice is cached
        container.replace_singleton(Service, NewImpl)
        if trigger == "lookup":
            cache.get_cached_registration(key, "app")
        else:
            cache.refresh()

    worker = threading.Thread(target=story, daemon=True)
    worker.start()
    worker.join(5)
    return finalized.is_set(), done.is_set(), worker.is_alive()


def test_a_finalizer_run_by_a_rebuild_can_look_up_names_without_a_hang(monkeypatch):
    """Review round 9: a rebuild dropped the old map and choices while holding the cache's lock. When
    that dropped the last reference to an object whose __del__ builds an injected object (a name
    lookup), the lookup waited for the same lock in the same thread - a hang."""
    lookup = lambda: autowired._NAME_MAP_CACHE.get_cached_registration(  # noqa: E731
        autowired._field_key("service_instance"), "app")
    finalized, done, running = _rebuild_with_a_finalizer(monkeypatch, lookup, "lookup")
    assert finalized, "the old registration was not dropped by the rebuild"
    assert done and not running, "a finalizer's lookup hung on the cache's lock"


@pytest.mark.parametrize("trigger", ["lookup", "refresh"])
def test_no_user_finalizer_runs_under_the_cache_s_lock(monkeypatch, trigger):
    """The old map and choices are dropped after the lock, on both rebuild paths: a __del__ that
    itself takes the cache's lock (here: refresh()) never waits for its own thread."""
    refresh = lambda: autowired._NAME_MAP_CACHE.refresh()  # noqa: E731
    finalized, done, running = _rebuild_with_a_finalizer(monkeypatch, refresh, trigger)
    assert finalized, "the old registration was not dropped by the rebuild"
    assert done and not running, "a finalizer waited for the cache's lock"



@pytest.mark.parametrize("debug", [True, False])
def test_the_name_map_is_logged_once_per_rebuild(monkeypatch, debug):
    """R15 (pahalı kapı, uzlaşma): in debug mode the name map block is written exactly ONCE per rebuild
    - the first build, one a registration change triggers, an explicit refresh() - and never while a
    library lock is held; with debug mode off, never. (The block used to be written under the cache
    lock; once kept and written after it, a second write from _rebuild would go unnoticed.)"""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(autowired, "DEBUG_MODE", debug)
    held_at_write = []

    class Count(logging.Handler):
        def emit(self, record):
            if record.getMessage() == "Name map built:":
                held_at_write.append(container_module._HELD.count)

    handler = Count(logging.DEBUG)
    log = logging.getLogger("py_autowired")
    old_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        container = Container()
        container.register_singleton(Service, OldImpl)
        cache = autowired._CachedNameMap(container)
        counts = []
        cache.get_raw_map()                                   # the first build
        counts.append(len(held_at_write))
        container.replace_singleton(Service, NewImpl)
        cache.get_raw_map()                                   # a rebuild a registration change triggers
        counts.append(len(held_at_write))
        cache.refresh()                                       # an explicit rebuild
        counts.append(len(held_at_write))
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
    assert counts == ([1, 2, 3] if debug else [0, 0, 0]), counts
    assert held_at_write == [0] * len(held_at_write), "written while a library lock was held"
