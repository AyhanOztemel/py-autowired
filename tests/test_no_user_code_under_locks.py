"""0.5.4 review rounds 9-10: no user code runs while the library holds one of its short locks. When a
registration is replaced, a scope ends or the default container changes, the last reference to a
user object may go - its __del__ then runs where that happens. Under the registration lock, a
__del__ that resolves a singleton whose creation lock another thread holds (that thread waiting for
the registration lock) hangs both, and the wait-for graph does not see the registration lock. So
every such reference is let go after the lock.

Each test records, inside __del__, whether this thread still held the lock (deterministic: one
thread, the object freed where its last reference goes)."""

import gc

from py_autowired import Container, LifetimeScope


class Service:
    pass


class Other(Service):
    pass


class Alias:
    pass


class Unrelated:  # registered last: the replaced registration is not the last one iterated
    pass


def _holder(probe, seen):
    class Holder(Service):
        def __del__(self):
            seen.append(probe())
    return Holder()


def _registration_lock_owned(container):
    return lambda: container._registration_lock._is_owned()


def test_replace_lets_the_old_registration_go_after_the_lock():
    container, seen = Container(), []
    container.register_instance(Service, _holder(_registration_lock_owned(container), seen))
    container.register(Unrelated)
    gc.collect()
    container.replace(Service, Other)
    assert seen, "the replaced object was not freed by replace()"
    assert seen == [False], "a __del__ ran under the registration lock"


def test_replace_lets_the_earlier_registrations_go_after_the_lock():
    container, seen = Container(), []
    probe = _registration_lock_owned(container)
    container.register_instance(Service, _holder(probe, seen))
    container.register_instance(Service, _holder(probe, seen))  # the first one is kept as earlier
    gc.collect()
    container.replace(Service, Other)
    assert len(seen) == 2, f"not every replaced object was freed: {seen}"
    assert seen == [False, False], "a __del__ ran under the registration lock"


def test_replace_lets_an_alias_s_old_registration_go_after_the_lock():
    """register() leaves an alias on the earlier registration; replace() moves the alias along."""
    container, seen = Container(), []
    container.register_instance(Service, _holder(_registration_lock_owned(container), seen))
    container.register_alias(Alias, Service)
    container.register(Service, Other, LifetimeScope.SINGLETON)  # the alias keeps the instance one
    container.register(Unrelated)
    gc.collect()
    seen.clear()
    container.replace(Service, Other)
    assert seen, "the alias's old registration was not freed by replace()"
    assert seen == [False], "a __del__ ran under the registration lock"


def test_an_alias_written_over_another_registration_lets_it_go_after_the_lock():
    container, seen = Container(), []
    container.register_instance(Alias, _holder(_registration_lock_owned(container), seen))
    container.register_instance(Service, Other())
    gc.collect()
    container.register_alias(Alias, Service)
    assert seen, "the overwritten registration was not freed by register_alias()"
    assert seen == [False], "a __del__ ran under the registration lock"


def test_a_closed_scope_lets_its_objects_without_a_finalizer_go_after_the_lock():
    container, seen = Container(), []
    states = []

    def probe():
        return states[0].lock._is_owned()

    class Plain:  # no close/dispose: kept for the closing code until the close ends
        def __del__(self):
            seen.append(probe())

    container.register_scoped(Plain)
    scope = container.create_scope()
    with scope:
        states.append(container.current_scope)
        container.resolve(Plain)
    assert seen, "the scope's object was not freed when the scope closed"
    assert seen == [False], "a __del__ ran under the scope lock"


def test_a_new_default_container_lets_the_old_one_go_after_the_lock():
    seen = []
    old = Container()
    old.register_instance(Service, _holder(lambda: Container._provider_lock.locked(), seen))
    previous = Container._instance
    try:
        Container.set_default(old)
        del old
        gc.collect()
        Container.set_default(Container())
        gc.collect()
        assert seen, "the old default container's object was not freed"
        assert seen == [False], "a __del__ ran under the provider lock"
    finally:
        Container._instance = previous


# ---- the library lock (_GLOBAL_LOCK) and the web bridges' startup lock (review round 10, C) ----

import threading  # noqa: E402

import pytest  # noqa: E402

from py_autowired import activate_container, auto_inject, autowired  # noqa: E402
from py_autowired import refresh as refresh_name_map  # noqa: E402


def _library_lock_owned():
    return autowired._GLOBAL_LOCK._owner == threading.get_ident()


@pytest.fixture
def no_active_cache(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)


def _old_active_container(seen, probe):
    """An active container whose only owner is the active name map (the test keeps no reference)."""
    old = Container()
    old.register_instance(Service, _holder(probe, seen))
    activate_container(old)
    autowired._NAME_MAP_CACHE.get_raw_map()


def test_activating_another_container_lets_the_old_cache_go_after_the_library_lock(no_active_cache):
    seen = []
    _old_active_container(seen, _library_lock_owned)
    gc.collect()
    activate_container(Container())
    assert seen, "the old container's object was not freed"
    assert seen == [False], "a __del__ ran under the library lock"


def test_auto_inject_lets_the_old_cache_go_after_the_library_lock(no_active_cache, tmp_path):
    seen = []
    _old_active_container(seen, _library_lock_owned)
    gc.collect()
    auto_inject(Container(), root_dir=str(tmp_path))
    assert seen, "the old container's object was not freed"
    assert seen == [False], "a __del__ ran under the library lock"


def _replaced_in_the_active_map(seen):
    container = Container()
    container.register_instance(Service, _holder(_library_lock_owned, seen))
    container.register(Unrelated)
    activate_container(container)
    cache = autowired._NAME_MAP_CACHE
    cache.get_cached_registration(autowired._field_key("service_instance"), "app")
    container.replace(Service, Other)  # the old registration lives on only in the active map
    gc.collect()
    return container


def test_refresh_lets_the_old_map_go_after_the_library_lock(no_active_cache):
    seen = []
    _replaced_in_the_active_map(seen)
    refresh_name_map()
    assert seen, "the replaced object was not freed by refresh()"
    assert seen == [False], "a __del__ ran under the library lock"


def test_a_rebuild_inside_the_library_lock_lets_the_old_map_go_after_it(no_active_cache):
    """activate_container() of the active container drains late imports under the library lock;
    that rebuilds the map changed meanwhile."""
    seen = []
    container = _replaced_in_the_active_map(seen)
    activate_container(container)
    assert seen, "the replaced object was not freed by the rebuild"
    assert seen == [False], "a __del__ ran under the library lock"


def test_the_web_startup_lets_the_old_cache_go_after_its_lock(no_active_cache, tmp_path):
    from py_autowired._web import Startup

    seen = []
    startup = Startup(Container(), str(tmp_path), None, False)
    _old_active_container(seen, lambda: startup._lock.locked() or _library_lock_owned())
    gc.collect()
    startup.ensure()
    assert seen, "the old container's object was not freed"
    assert seen == [False], "a __del__ ran under the startup lock or the library lock"


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")  # the broken __del__, reported
def test_a_failing_drop_never_keeps_the_other_objects(no_active_cache):
    """The objects let go under the library lock are all dropped after it, also when one __del__
    raises (Python reports that one and goes on)."""
    seen = []

    class Raises:
        def __del__(self):
            seen.append("raised")
            raise RuntimeError("a broken __del__")

    class Records:
        def __del__(self):
            seen.append(_library_lock_owned())

    with autowired._GLOBAL_LOCK:
        autowired._GLOBAL_LOCK.let_go(Raises())
        autowired._GLOBAL_LOCK.let_go(Records())
        assert seen == [], "an object was dropped under the lock"
    assert sorted(map(str, seen)) == ["False", "raised"], seen


# ---- review round 12: no registration lock is taken while a creation lock is held ----

import abc  # noqa: E402
from typing import Generic, TypeVar  # noqa: E402


def _hold_the_registration_lock_and_resolve(container, service, creating):
    """T2 holds the registration lock (as user code under it would: a key's __hash__) and resolves
    ``service``, whose creation T1 has started. Returns True when both threads finished."""
    held = threading.Event()

    def t2():
        with container._registration_lock:
            held.set()
            assert creating.wait(5)
            container.resolve(service)

    def t1():
        assert held.wait(5)
        container.resolve(service)

    threads = [threading.Thread(target=t2, daemon=True), threading.Thread(target=t1, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    return not any(thread.is_alive() for thread in threads)


def test_a_late_discovered_implementation_takes_no_registration_lock_under_a_creation_lock(monkeypatch):
    """A singleton interface registered before its implementation was imported: the creation found
    the implementation and wrote it under the registration lock while it held the creation lock."""
    class Port(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container = Container()
    container.register_singleton(Port)

    class Adapter(Port):  # imported after the registration
        def run(self):
            return "ok"

    creating = threading.Event()
    real = Container._discovered_implementation

    def discovered(service_type, *, defer):
        found = real(service_type, defer=defer)
        if not defer:
            creating.set()  # T1 holds Port's creation lock now
        return found
    monkeypatch.setattr(Container, "_discovered_implementation", staticmethod(discovered))
    assert _hold_the_registration_lock_and_resolve(container, Port, creating), \
        "a creation lock and the registration lock were taken in opposite orders (a hang)"


def test_a_closed_generic_takes_no_registration_lock_under_a_creation_lock():
    T = TypeVar("T")

    class Repo(Generic[T]):
        pass

    class SqlRepo(Repo[T]):
        pass

    class User:
        pass

    class Service:
        pass

    container = Container()
    container.register_open_generic(Repo, SqlRepo)
    creating = threading.Event()

    def make(c):
        creating.set()  # T1 holds Service's creation lock now
        c.resolve(Repo[User])
        return Service()
    container.register_factory(Service, make, LifetimeScope.SINGLETON)
    assert _hold_the_registration_lock_and_resolve(container, Service, creating), \
        "a creation lock and the registration lock were taken in opposite orders (a hang)"


# ---- review round 12 consensus: the change count's lock contract (not a lost-update race test) ----

class _WatchedCount(int):
    """A change count that records, at every increment, whether its own lock is held."""

    def __new__(cls, value, container, log):
        count = int.__new__(cls, value)
        count.container, count.log = container, log
        return count

    def __add__(self, other):
        self.log.append(self.container._change_lock.locked())
        return _WatchedCount(int(self) + other, self.container, self.log)


def test_lock_contract_every_change_count_increment_holds_the_change_lock():
    """Lock contract (kilit sozlesmesi): every place that moves the change count does it under
    the count's own leaf lock (register, alias, completed implementations, a late-discovered
    implementation through a real resolve()). This pins the contract; it does not show a lost
    update between two racing increments end to end."""
    class Port(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    class Late(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container, log = Container(), []
    container.register_singleton(Port)
    container.register_singleton(Late)

    class Adapter(Port):  # imported after the registration
        def run(self):
            return "ok"

    class LateAdapter(Late):
        def run(self):
            return "late"

    container._changes = _WatchedCount(container._changes, container, log)
    container.register(Unrelated)                          # _store_registration
    container.register_alias(Alias, Unrelated)             # register_alias
    container._complete_implementations()                  # completed implementations (Port)
    assert container.resolve(Late).run() == "late"         # a late discovery, under its creation lock
    assert len(log) == 4, f"increments seen: {log}"
    assert all(log), f"an increment ran without the change lock: {log}"


def test_the_check_never_runs_a_registered_type_s_getattribute(monkeypatch, caplog):
    """Review round 12 consensus (RW11b), without FastAPI: a registered type that overrides
    __getattribute__ - the check reads its dictionary without asking it, and still reports."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    asked = []

    class Guarded:
        helper_on_class = None  # not a field: never reported

        def __init__(self):
            self.clock_instance = None
            self.helper = None  # not a field: never reported

        def __getattribute__(self, name):
            asked.append(name)
            return object.__getattribute__(self, name)

    guarded = Guarded()
    asked.clear()
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(guarded)
    assert asked == [], f"the type's __getattribute__ ran: {asked}"
    lines = [r.getMessage() for r in caplog.records if "Guarded." in r.getMessage()]
    assert len(lines) == 1 and "Guarded.clock_instance" in lines[0], lines


def test_each_none_field_of_a_registered_type_is_reported_once_also_on_a_later_object(monkeypatch, caplog):
    """Review round 12 follow-up: the note was per class and reason, so a later object of the same
    class with ANOTHER None field said nothing. It is per field name now."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)

    class TwoFields:
        def __init__(self, a, b):
            self.a_instance, self.b_instance = a, b

    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(TwoFields(None, object()))  # a is None
        autowired._warn_unfilled_registered(TwoFields(object(), None))  # b is None
        autowired._warn_unfilled_registered(TwoFields(None, None))      # both already reported
    lines = [r.getMessage() for r in caplog.records if "TwoFields." in r.getMessage()]
    assert any("TwoFields.a_instance" in m for m in lines), lines
    assert any("TwoFields.b_instance" in m for m in lines), f"the later object's None field was not reported: {lines}"
    assert len(lines) == 2, lines


def test_a_lazy_value_never_takes_the_registration_lock_under_its_gate(monkeypatch):
    """Review round 13: Lazy.value holds its gate (a creation lock) while it resolves; the change
    count of a late-discovered implementation was then moved under the registration lock - a
    thread holding the registration lock (a key's __hash__) and reading the same Lazy hung both."""
    from py_autowired import Lazy

    class Late(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    container = Container()
    container.register_singleton(Late)

    class LateAdapter(Late):  # imported after the registration
        def run(self):
            return "late"

    lazy = Lazy(container, Late)
    creating, held = threading.Event(), threading.Event()
    real = Container._discovered_implementation

    def discovered(service_type, *, defer):
        found = real(service_type, defer=defer)
        if not defer:
            creating.set()  # T1 holds the Lazy's gate now
            assert held.wait(5)
        return found
    monkeypatch.setattr(Container, "_discovered_implementation", staticmethod(discovered))

    def t2():
        with container._registration_lock:
            held.set()
            assert creating.wait(5)
            lazy.value

    threads = [threading.Thread(target=lambda: lazy.value, daemon=True), threading.Thread(target=t2, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads), \
        "a Lazy's gate and the registration lock were taken in opposite orders (a hang)"


def test_a_none_field_of_a_later_object_is_reported_after_a_filled_first_one(monkeypatch, caplog):
    """Review round 13 (Y, Z): the first object of the class had every field filled - the class was
    noted as looked at, and a later object's None field said nothing."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)

    class OneField:
        def __init__(self, value):
            self.clock_instance = value

    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(OneField(object()))  # filled: nothing to say
        autowired._warn_unfilled_registered(OneField(None))      # None: reported
    assert any("OneField.clock_instance" in r.getMessage() for r in caplog.records), \
        "the later object's None field was not reported"


def test_a_class_level_field_added_after_the_first_object_is_still_reported(monkeypatch, caplog):
    """Impact map of round 13: the class chain's field names were found once per class and kept;
    a None field the class got afterwards (a test or a plugin setting it) was never reported."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)

    class Grows:
        pass

    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(Grows())  # no field yet
        Grows.clock_instance = None
        autowired._warn_unfilled_registered(Grows())
    assert any("Grows.clock_instance" in r.getMessage() for r in caplog.records), \
        "a class-level None field added later was not reported"


def test_only_field_names_are_looked_at():
    """RW18/RW19 consensus: a None attribute whose name is not a field name (on the object or on its
    class) is never a candidate - only *_instance names are."""
    class Mixed:
        helper_on_class = None
        repo_instance_count = None  # looks close, is not a field name

        def __init__(self):
            self.clock_instance = None
            self.helper = None
            self.cache = None

    mixed = Mixed()
    merged = autowired._class_chain(Mixed)[0]
    own, why = autowired._plain_fields_source(mixed, Mixed, merged)
    assert why is None, why
    assert autowired._unfilled_fields(mixed, Mixed, merged, own) == ["clock_instance"]


def test_a_type_with_its_own_dict_says_its_fields_cannot_be_checked(monkeypatch, caplog):
    """Review round 14 (Y, Z): the class shadows __dict__ with a property; the object's own
    dictionary is not read (that would run user code), so a None field set in __init__ went
    unreported and nothing said the fields were not checked. Now one WARNING per class says so."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    asked = []

    class OwnDict:
        def __init__(self):
            self.repo_instance = None

        @property
        def __dict__(self):
            asked.append("__dict__")
            return {}

    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(OwnDict())
        autowired._warn_unfilled_registered(OwnDict())
    lines = [r.getMessage() for r in caplog.records if "OwnDict" in r.getMessage()]
    assert asked == [], "the type's __dict__ property ran"
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


def test_a_slots_only_type_is_checked_without_that_warning(monkeypatch, caplog):
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)

    class Slots:
        __slots__ = ("repo_instance",)

        def __init__(self):
            self.repo_instance = None

    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(Slots())
    lines = [r.getMessage() for r in caplog.records if "Slots" in r.getMessage()]
    assert len(lines) == 1 and "Slots.repo_instance" in lines[0], lines


def test_a_metaclass_that_redefines_class_attributes_runs_no_code_in_the_check(monkeypatch, caplog):
    """Impact map 15, open finding: the check read the class's namespace, MRO and module name through
    normal attribute access - a metaclass defining them as properties ran its own code. They are
    read through type's own descriptors now (as inspect.getattr_static does); the report is unchanged."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    asked = []

    class Meta(type):
        @property
        def __dict__(cls):
            asked.append("__dict__")
            return {}

        @property
        def __mro__(cls):
            asked.append("__mro__")
            return (object,)

        @property
        def __module__(cls):
            asked.append("__module__")
            return "fake"

    class Tricky(metaclass=Meta):
        clock_instance = None

        def __init__(self):
            self.repo_instance = None

    tricky = Tricky()
    asked.clear()
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(tricky)
    lines = sorted(r.getMessage().split(":")[0] for r in caplog.records if "Tricky." in r.getMessage())
    assert asked == [], f"the metaclass's code ran: {asked}"
    assert lines == [f"{__name__}.{Tricky.__qualname__}.clock_instance",
                     f"{__name__}.{Tricky.__qualname__}.repo_instance"], lines


def test_a_field_like_descriptor_makes_the_fields_unchecked_and_plain_values_keep_their_order():
    """Round 15 decision: a descriptor under a field-like name (a property, one with __delete__ only)
    is not read - the class's fields cannot be checked. With plain values only, the object's own
    value still comes before the class value."""
    class DeleteOnly:
        def __get__(self, obj, owner=None):
            return "a cache"

        def __delete__(self, obj):
            pass

    class WithProperty:
        @property
        def repo_instance(self):
            return "a repo"

    class WithDeleteOnly:
        cache_instance = DeleteOnly()

    for cls in (WithProperty, WithDeleteOnly):
        own, why = autowired._plain_fields_source(cls(), cls, autowired._class_chain(cls)[0])
        assert own is None and "descriptor" in why, (cls, why)

    class Plain:
        clock_instance = None  # the object sets its own value below

        def __init__(self):
            self.clock_instance = "a clock"

    plain = Plain()
    merged = autowired._class_chain(Plain)[0]
    own, why = autowired._plain_fields_source(plain, Plain, merged)
    assert why is None and autowired._unfilled_fields(plain, Plain, merged, own) == []


def test_a_metaclass_getattribute_runs_no_code_in_the_check(monkeypatch, caplog):
    """A metaclass overriding __getattribute__ sees every normal attribute access on the class
    (__qualname__ and __module__ included); the check reads none of them that way."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    asked = []

    class Watching(type):
        def __getattribute__(cls, name):
            asked.append(name)
            return type.__getattribute__(cls, name)

    class Watched(metaclass=Watching):
        def __init__(self):
            self.repo_instance = None

    watched = Watched()
    label = type.__dict__["__qualname__"].__get__(Watched)
    asked.clear()
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(watched)
    assert asked == [], f"the metaclass's __getattribute__ ran: {asked}"
    assert any(f"{label}.repo_instance" in r.getMessage() for r in caplog.records)


# ---- RW5c consensus: the once-only rule holds under races and re-entry ----

def test_two_threads_never_write_the_same_once_only_warning_twice(monkeypatch, caplog):
    """Review RW5c (Y, Z): _warn_once checked the key and added it in two steps - two threads (the
    free-threaded build) both passed the check and wrote the same WARNING twice."""
    import logging

    from py_autowired import container as container_module

    meet = threading.Barrier(2)

    class Racing(set):
        def __contains__(self, key):
            found = set.__contains__(self, key)
            try:
                meet.wait(0.5)  # both threads have checked (only possible without one step)
            except threading.BrokenBarrierError:
                pass
            return found

    monkeypatch.setattr(container_module, "_WARNED", Racing())
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        threads = [threading.Thread(target=container_module._warn_once, args=(("race",), "race %s", 1))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    lines = [r.getMessage() for r in caplog.records if r.getMessage() == "race 1"]
    assert len(lines) == 1, f"the once-only WARNING was written {len(lines)} times"


@pytest.mark.parametrize("startup", [False, True])
def test_a_registered_field_found_twice_before_it_is_noted_is_reported_once(monkeypatch, caplog, startup):
    """RW5c (Y, option 1): a second check of the same class finds the field before the first one
    notes it (re-entry) - the class still gets exactly one WARNING, for both reasons."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", object() if startup else None)

    class Twice:
        def __init__(self):
            self.clock_instance = None

    real = autowired._unfilled_fields
    entered = []

    def unfilled(instance, cls, merged, own, skip):
        found = real(instance, cls, merged, own, skip)
        if not entered:
            entered.append(True)
            autowired._warn_unfilled_registered(Twice())  # before the outer call notes the field
        return found
    monkeypatch.setattr(autowired, "_unfilled_fields", unfilled)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(Twice())
    lines = [r.getMessage() for r in caplog.records if "Twice." in r.getMessage()]
    assert len(lines) == 1, lines


def test_a_once_only_key_already_used_keeps_the_registered_warning_quiet(monkeypatch, caplog):
    """RW5c (Z, option 2): the warning's key is stable - when it is already used, nothing is written."""
    import logging

    from py_autowired import container as container_module

    class Quiet:
        def __init__(self):
            self.clock_instance = None

    module = type.__dict__["__module__"].__get__(Quiet)
    name = type.__dict__["__qualname__"].__get__(Quiet)
    monkeypatch.setattr(container_module, "_WARNED", {("registered-unpatched-no-startup", module, name,
                                                        "clock_instance")})
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(Quiet())
    assert not [r.getMessage() for r in caplog.records if "Quiet." in r.getMessage()]


def test_no_user_hash_runs_under_the_once_only_lock(monkeypatch):
    """The once-only check is a leaf lock: a key part that is not a plain string or number is turned
    into a plain string first, so no user __hash__/__eq__ runs while it is held."""
    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    seen = []

    class Odd:
        def __hash__(self):
            lock = getattr(container_module, "_WARNED_LOCK", None)
            seen.append(lock is not None and lock.locked())
            return 1

        def __eq__(self, other):
            return self is other

    container_module._warn_once(("odd", Odd()), "odd")
    assert seen == [] or not any(seen), f"a user __hash__ ran under the once-only lock: {seen}"
    assert "_WARNED_LOCK" in vars(container_module), "the once-only check has no lock"


def test_two_different_odd_key_parts_both_warn_even_when_one_is_gone(monkeypatch, caplog):
    """A key with a part that is not a plain string or number is not noted: nothing of the user is
    kept (no leak), and a later object - even at the same address - is never silenced."""
    import gc
    import logging
    import weakref

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())

    class Odd:
        pass

    first = Odd()
    alive = weakref.ref(first)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        container_module._warn_once(("odd", first), "odd first")
        del first
        gc.collect()
        second = Odd()
        container_module._warn_once(("odd", second), "odd second")
    assert alive() is None, "the once-only check kept a user object alive (a leak)"
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("odd ")]
    assert lines == ["odd first", "odd second"], lines


# ---- review round 15: only plain values are read; anything else is "cannot be checked" ----

def _check(instance, monkeypatch, caplog):
    """Run the registered-type check on ``instance``; return the WARNING lines about its class."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    label = type.__dict__["__qualname__"].__get__(type(instance))
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        autowired._warn_unfilled_registered(instance)
    return [r.getMessage() for r in caplog.records if label + ":" in r.getMessage()
            or label + "." in r.getMessage()]


def test_a_dict_subclass_as_the_object_s_dictionary_is_not_read(monkeypatch, caplog):
    """Y, Z (round 15): the object's __dict__ is a dict subclass whose get() hides the None field.
    Its get() never runs; the class says its fields cannot be checked."""
    asked = []

    class Hiding(dict):
        def get(self, key, default=None):
            asked.append(key)
            return "something"

    class WithSubDict:
        def __init__(self):
            self.dep_instance = None

    instance = WithSubDict()
    instance.__dict__ = Hiding(instance.__dict__)
    lines = _check(instance, monkeypatch, caplog)
    assert asked == [], f"the dict subclass's get() ran: {asked}"
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


def test_a_set_only_descriptor_named_like_a_field_says_cannot_be_checked(monkeypatch, caplog):
    """Y, Z (round 15): a descriptor with __set__ only keeps the value in the object's dictionary;
    the None field was skipped silently. Now: cannot be checked, never silent."""
    class SetOnly:
        def __set__(self, obj, value):
            obj.__dict__["service_instance"] = value

    class Holder:
        service_instance = SetOnly()

        def __init__(self):
            self.service_instance = None

    lines = _check(Holder(), monkeypatch, caplog)
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


def test_a_class_level_dict_set_to_none_says_cannot_be_checked(monkeypatch, caplog):
    """Y (round 15): `__dict__ = None` in the class body was taken for "no __dict__"; the None field
    set in __init__ was neither read nor reported."""
    class NoneDict:
        __dict__ = None

        def __init__(self):
            self.service_instance = None

    lines = _check(NoneDict(), monkeypatch, caplog)
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


def test_a_key_part_whose_type_claims_equality_with_str_runs_no_user_hash_under_the_lock(monkeypatch):
    """Y (round 15): `type(part) in (...)` compared types with ==; a metaclass saying it equals str
    let the part through, and its __hash__ ran under the once-only lock."""
    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    seen = []

    class Liar(type):
        def __eq__(cls, other):
            return True

        __hash__ = type.__hash__

    class Part(metaclass=Liar):
        def __hash__(self):
            seen.append(container_module._WARNED_LOCK.locked())
            return 1

    container_module._warn_once(("liar", Part()), "liar")
    assert not any(seen), f"a user __hash__ ran under the once-only lock: {seen}"


def test_an_odd_key_part_is_never_kept_and_its_warning_is_written_every_time(monkeypatch, caplog):
    """Round 15 decision: a key with a part that is not exactly str/int/None is not noted - nothing
    of the user is kept or hashed under the lock, and the WARNING is written each time."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())

    class Odd:
        pass

    odd = Odd()
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        container_module._warn_once(("odd", odd), "odd")
        container_module._warn_once(("odd", odd), "odd")
    assert [r.getMessage() for r in caplog.records].count("odd") == 2
    assert container_module._WARNED == set(), "a key with a user object was kept"


def test_a_none_key_part_is_still_noted_once(monkeypatch, caplog):
    """getattr(owner, "__module__", "?") can be None (some C objects): such a key is plain and noted."""
    import logging

    from py_autowired import container as container_module

    monkeypatch.setattr(container_module, "_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        container_module._warn_once(("none-part", None, "name"), "none part")
        container_module._warn_once(("none-part", None, "name"), "none part")
    assert [r.getMessage() for r in caplog.records].count("none part") == 1


# ---- review round 16: a dictionary key that is not exactly a str ----

class _SubStr(str):
    pass


def test_a_field_held_under_a_str_subclass_key_says_cannot_be_checked(monkeypatch, caplog):
    """Y, Z (round 16): `repo_instance = None` kept under a str-subclass key fell out of the name
    filter - silently. Now the fields cannot be checked, and that is said."""
    class Holder:
        pass

    holder = Holder()
    holder.__dict__[_SubStr("repo_instance")] = None
    lines = _check(holder, monkeypatch, caplog)
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


def test_a_same_hash_key_s_eq_never_runs(monkeypatch, caplog):
    """Y (round 16): a key with the same hash as a field name made dict.get run its __eq__."""
    asked = []

    class Twin:
        def __hash__(self):
            return hash("clock_instance")

        def __eq__(self, other):
            asked.append("__eq__")
            return False

    class Holder:
        clock_instance = None

    holder = Holder()
    holder.__dict__[Twin()] = 1
    lines = _check(holder, monkeypatch, caplog)
    assert asked == [], "a dictionary key's __eq__ ran"
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


def test_a_class_dict_with_a_str_subclass_key_says_cannot_be_checked(monkeypatch, caplog):
    """Impact map 17: a class built with type() (or a metaclass's __prepare__) keeps a str-subclass
    key in its own dictionary; the class-level None field fell out of the filter - silently."""
    Built = type("Built", (), {_SubStr("clock_instance"): None})
    lines = _check(Built(), monkeypatch, caplog)
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


# ---- review round 17: one plain snapshot of every mapping the check looks at ----

def test_an_odd_key_in_a_field_value_s_type_never_runs_its_eq(monkeypatch, caplog):
    """Z (round 17): the descriptor test looked into the dictionaries of the field VALUE's type; a
    str-subclass key with the same hash as "__get__" ran its __eq__ there."""
    asked = []

    class Twin(str):
        def __hash__(self):
            return hash("__get__")

        def __eq__(self, other):
            asked.append("__eq__")
            return str.__eq__(self, other)

    ValueType = type("ValueType", (), {Twin("not_a_hook"): 1})

    class Holder:
        clock_instance = ValueType()

    holder = Holder()
    asked.clear()  # only what the check itself does counts
    lines = _check(holder, monkeypatch, caplog)
    assert asked == [], "a key's __eq__ in the value's type ran"
    assert len(lines) == 1 and "cannot be checked" in lines[0], lines


def test_a_key_added_after_the_check_is_never_compared(monkeypatch, caplog):
    """Y, Z (round 17): the object's dictionary was checked and then read again live; another thread
    adding a same-hash str-subclass key in between made dict.get run that key's __eq__. Here the key
    is added deterministically right after the check has looked at the dictionary."""
    asked = []

    class Twin(str):
        def __hash__(self):
            return hash("clock_instance")

        def __eq__(self, other):
            asked.append("__eq__")
            return False

    class Holder:
        clock_instance = None

    holder = Holder()
    real = autowired._DEF_ATTR_RE
    added = []

    class AddsAKey:  # the first name match happens after the dictionary was checked
        def match(self, name):
            if not added:
                added.append(True)
                holder.__dict__[Twin("other")] = 1  # "another thread" adds a key
            return real.match(name)

    monkeypatch.setattr(autowired, "_DEF_ATTR_RE", AddsAKey())
    lines = _check(holder, monkeypatch, caplog)
    assert added, "the hook never ran"
    assert asked == [], "a key added after the check was compared by its __eq__"
    assert any("Holder.clock_instance" in m for m in lines), lines


def test_a_subclass_value_wins_over_its_base_s_none(monkeypatch, caplog):
    """The class chain's snapshots are merged as attribute lookup reads them: the first class in
    the MRO wins - a subclass that gives the field a value is not reported for its base's None."""
    class Base:
        clock_instance = None

    class Sub(Base):
        clock_instance = "a clock"

    assert _check(Sub(), monkeypatch, caplog) == []


# ---- review round 18 ----

def test_the_object_s_dictionary_is_read_through_the_checked_descriptor(monkeypatch, caplog):
    """Z (round 18): after the class chain was copied, the object's __dict__ was read live; a thread
    switching its __class__ to a class with a __dict__ property in between ran that getter."""
    asked = []

    class Holder:
        def __init__(self):
            self.clock_instance = None

    class Switched(Holder):
        @property
        def __dict__(self):
            asked.append("__dict__")
            return {}

    holder = Holder()
    real = autowired._plain_snapshot
    flipped = []

    def snapshot(mapping):
        copy = real(mapping)
        if not flipped and copy is not None and copy.get("__init__") is Holder.__dict__["__init__"]:
            flipped.append(True)
            holder.__class__ = Switched  # "another thread", right after the class was copied
        return copy
    monkeypatch.setattr(autowired, "_plain_snapshot", snapshot)
    lines = _check(holder, monkeypatch, caplog)
    assert flipped, "the hook never ran"
    assert asked == [], "the __dict__ property of the switched class ran"
    assert any("clock_instance" in m for m in lines), lines


def test_a_value_type_is_copied_once_per_check(monkeypatch, caplog):
    """Y, Z (round 18, small): the dictionaries of a field value's type were copied for every field;
    within one check each type is copied once (no cache across checks)."""
    copies = []
    real = autowired._plain_snapshot

    def snapshot(mapping):
        copy = real(mapping)
        if copy is not None and copy.get("bit_length") is int.__dict__["bit_length"]:
            copies.append(1)
        return copy
    monkeypatch.setattr(autowired, "_plain_snapshot", snapshot)

    class ManyInts:
        a_instance = b_instance = c_instance = d_instance = 1

    _check(ManyInts(), monkeypatch, caplog)
    assert len(copies) == 1, f"int's dictionary was copied {len(copies)} times in one check"
    _check(ManyInts(), monkeypatch, caplog)
    assert len(copies) == 2, "a copy was reused across checks"


def test_a_patched_class_is_skipped_without_touching_any_dictionary(monkeypatch, caplog):
    """Round 18 decision: a class the library patched is skipped from its own registry - no
    dictionary of the class is copied or read."""
    from py_autowired import Container

    copies = []
    real = autowired._plain_snapshot

    class Patched:
        def __init__(self):
            self.clock_instance = None

    autowired._patch_init(Patched, Container(), {})
    patched = Patched()
    caplog.clear()  # only what the check itself writes counts (not the patched __init__'s WARNING)
    monkeypatch.setattr(autowired, "_plain_snapshot", lambda m: copies.append(1) or real(m))
    assert _check(patched, monkeypatch, caplog) == []
    assert copies == [], "a dictionary of a patched class was copied"


def test_a_class_marked_by_hand_but_never_patched_is_checked(monkeypatch, caplog):
    """Round 18 decision (gain): `__ai_ioc_init__ = True` set by hand (or copied) on a class the
    library never patched no longer hides its None fields."""
    class MarkedByHand:
        __ai_ioc_init__ = True

        def __init__(self):
            self.clock_instance = None

    lines = _check(MarkedByHand(), monkeypatch, caplog)
    assert any("MarkedByHand.clock_instance" in m for m in lines), lines


def test_a_registry_entry_for_a_reused_id_never_skips_another_class(monkeypatch, caplog):
    """The patched registry is keyed by id: an entry whose class is not this very class (an id
    reused after a patched class died) never makes the check skip it."""
    import weakref

    class Other:
        pass

    class Checked:
        def __init__(self):
            self.clock_instance = None

    monkeypatch.setitem(autowired._PATCHED_IDS, id(Checked), weakref.ref(Other))
    lines = _check(Checked(), monkeypatch, caplog)
    assert any("Checked.clock_instance" in m for m in lines), lines


def test_a_patched_class_that_dies_leaves_no_registry_entry():
    import gc

    from py_autowired import Container

    def make():
        class Short:
            def __init__(self):
                self.clock_instance = None
        autowired._patch_init(Short, Container(), {})
        return id(Short)

    key = make()
    gc.collect()
    assert key not in autowired._PATCHED_IDS, "a dead class kept its registry entry"
