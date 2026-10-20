"""Fifth wave of mutation-driven tests (manual review of round 5 survivors)."""

import asyncio
import sys
import threading
import types
import uuid
from typing import Annotated, Generic, TypeVar

import pytest

from py_autowired import (
    AsyncDisposalRequiredError, Container, DuplicateServiceRegistrationError, Keyed,
    LifetimeScope, Provider, ServiceRegistration, autowired, validate_autowiring,
    AutowiringValidationError,
)
from py_autowired import container as core

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


class Dep:
    pass


class IClock:
    pass


class UtcClock(IClock):
    pass


# ---- class-level default container ------------------------------------------
def test_default_container_provider_and_set_default(monkeypatch):
    monkeypatch.setattr(Container, "_instance", None)
    with pytest.raises(core.ServiceNotRegisteredError):
        Container.provider(Dep)                   # creates the default container lazily
    created = Container._instance
    assert isinstance(created, Container)
    custom = Container()
    custom.register_transient(Dep)
    Container.set_default(custom)
    assert isinstance(Container.provider(Dep), Dep) and Container._instance is custom


# ---- scope enter/exit edge cases --------------------------------------------
def test_exiting_a_scope_that_was_never_entered_is_harmless():
    container = Container()
    scope = container.create_scope()
    scope.__exit__(None, None, None)
    asyncio.run(container.create_scope().__aexit__(None, None, None))
    assert container.current_scope is None


# ---- Keyed equality uses values, not identity -------------------------------
def test_keyed_equality_by_value():
    key_a = "".join(["u", "tc"])
    key_b = "".join(["ut", "c"])
    assert key_a is not key_b
    assert Keyed(key_a) == Keyed(key_b)
    big_a, big_b = int("100000"), int("100000")
    assert Keyed(big_a) == Keyed(big_b)


# ---- module similarity ------------------------------------------------------
def test_module_similarity_counts_only_the_common_prefix():
    autowired._calculate_module_similarity.cache_clear()
    assert autowired._calculate_module_similarity("a.b.c", "a.x.c") == 1
    assert autowired._calculate_module_similarity("a.b.c", "a.b.d") == 2
    assert autowired._calculate_module_similarity("a.b", "a.b.c") == 2
    assert autowired._calculate_module_similarity("x", "y") == 0


# ---- discovery: names sorting before "__init__.py" are still imported -------
def test_uppercase_module_names_are_discovered(tmp_path, monkeypatch):
    name = f"up_{uuid.uuid4().hex[:8]}"
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "Models.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    assert autowired._discover_and_load_modules(tmp_path) == 1
    assert f"{name}.Models" in sys.modules


def test_auto_inject_adds_root_to_sys_path_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(tmp_path.resolve())])
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    autowired.auto_inject(Container(), root_dir=str(tmp_path))
    autowired.auto_inject(Container(), root_dir=str(tmp_path))
    assert sys.path.count(str(tmp_path.resolve())) == 1
    assert sys.path[0] == str(tmp_path.resolve())


# ---- annotations with several metadata items --------------------------------
def test_keyed_marker_found_among_other_metadata():
    assert core._keyed_marker(Annotated[IClock, "doc", Keyed("utc")]) == (IClock, "utc")
    assert core._keyed_marker(Annotated[IClock, Keyed("utc"), "doc"]) == (IClock, "utc")
    assert core._keyed_marker(Annotated[IClock, "doc"]) is None
    assert core._unwrap_annotation(Annotated[IClock, "doc", Keyed("x")]) is IClock


# ---- provider parameter followed by a normal one -----------------------------
class Mixed:
    def __init__(self, later: Provider[Dep], dep: Dep):
        self.later, self.dep = later, dep


def test_provider_parameter_does_not_stop_later_injection():
    container = Container()
    container.register_transient(Dep)
    container.register_transient(Mixed)
    mixed = container.resolve(Mixed)
    assert isinstance(mixed.dep, Dep) and isinstance(mixed.later(), Dep)


# ---- open generic: fewer implementation parameters than type arguments -------
class Map(Generic[K, V]):
    pass


class OneParamMap(Map[str, V]):
    pass


def test_typevar_map_requires_equal_counts_both_ways():
    container = Container()
    container.register_open_generic(Map, OneParamMap)
    assert container._closed_generic_registration(Map[str, int], cache=False).typevar_map is None


def test_is_registered_and_validation_do_not_populate_the_closed_cache():
    class Box(Generic[T]):
        pass

    container = Container()
    container.register_open_generic(Box)
    assert container.is_registered(Box[int]) is True
    assert container._closed_generics == {}


# ---- disposal details ---------------------------------------------------------
def test_additional_errors_noted_but_not_the_first_itself():
    first, second = ValueError("first-error"), LookupError("second-error")
    with pytest.raises(ValueError) as caught:
        core._raise_disposal_errors([first, second])
    notes = getattr(caught.value, "__notes__", [])
    assert any("second-error" in n for n in notes)
    assert not any("first-error" in n for n in notes)


def test_sync_finalizer_returning_plain_awaitable_requires_async_scope():
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()

        class Weird:
            def close(self):
                return future                       # awaitable without close()

        with pytest.raises(AsyncDisposalRequiredError):
            core._ScopeState._sync_finalize(Weird())
    finally:
        loop.close()


def test_async_close_reports_failures():
    class Bad:
        async def aclose(self):
            raise LookupError("async-bad")

    container = Container()
    container.register_singleton(Bad)
    container.resolve(Bad)
    with pytest.raises(LookupError, match="async-bad"):
        asyncio.run(container.aclose())


# ---- registration repr, alias rules, scoped_function -------------------------
def test_registration_repr_hides_the_lock():
    assert "lock" not in repr(ServiceRegistration(Dep))


def test_alias_rules():
    container = Container()
    container.register_transient(Dep)
    container.register_transient(IClock, UtcClock)
    container.register_alias(IClock, Dep)                     # non-strict: alias overrides
    assert type(container.resolve(IClock)) is Dep
    strict = Container(strict_registrations=True)
    strict.register_transient(Dep)
    strict.register_transient(IClock, UtcClock)
    with pytest.raises(DuplicateServiceRegistrationError):
        strict.register_alias(IClock, Dep)
    strict.register_alias(UtcClock, Dep)                      # new alias is fine
    assert type(strict.resolve(UtcClock)) is Dep


def test_scoped_function_preserves_metadata_and_opens_a_scope():
    container = Container()
    container.register_scoped(Dep)

    def handler():
        """Sync handler."""
        return container.resolve(Dep)

    async def ahandler():
        """Async handler."""
        return container.resolve(Dep)

    wrapped, awrapped = container.scoped_function(handler), container.scoped_function(ahandler)
    assert wrapped.__name__ == "handler" and wrapped.__doc__ == "Sync handler."
    assert awrapped.__name__ == "ahandler" and awrapped.__doc__ == "Async handler."
    assert isinstance(wrapped(), Dep) and isinstance(asyncio.run(awrapped()), Dep)


# ---- validation details ---------------------------------------------------------
def test_validation_message_names_the_deferred_inner_type_and_missing_implementation(monkeypatch):
    class Missing:
        pass

    class Holder:
        def __init__(self, x: Provider[Missing]):
            pass

    container = Container()
    container.register_transient(Holder)
    container._store_registration(ServiceRegistration(service_type=IClock))   # no implementation
    consumer = type("Consumer", (), {"__module__": "app.c", "holder_instance": None, "clock_instance": None})
    name_map = autowired._build_name_map_from_container(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._CachedNameMap(container))
    autowired._patch_setattr(consumer, container, name_map)
    autowired._patch_init(consumer, container, name_map)
    with pytest.raises(AutowiringValidationError) as caught:
        validate_autowiring(container)
    text = "\n".join(caught.value.issues)
    assert "Missing" in text and "Provider" not in text.split("defers an unregistered dependency")[-1]
    assert "IClock has no implementation" in text


# ---- ownership comparisons with foreign threads on both sides ------------------
@pytest.mark.parametrize("delta", [-1, 1])
def test_foreign_owner_on_either_side(monkeypatch, delta):
    container = Container()
    container.register_singleton(Dep)
    registration = container.registrations[Dep]
    me = threading.get_ident()
    owners = {}
    monkeypatch.setattr(core, "_LOCK_OWNERS", owners)
    owners[id(registration)] = (me + delta, 1)
    core.Container._note_owner(registration, me)
    assert owners[id(registration)] == (me, 1)
    owners[id(registration)] = (me + delta, 2)
    registration.lock.acquire()
    core.Container._release_creation(registration)
    assert owners[id(registration)] == (me + delta, 2)


@pytest.mark.parametrize("me", [1, 2, 3])
def test_wait_cycle_independent_of_thread_id_order(monkeypatch, me):
    others = [t for t in (1, 2, 3) if t != me]
    a, b = object(), object()
    monkeypatch.setattr(core, "_LOCK_OWNERS", {id(a): (others[0], 1), id(b): (me, 1)})
    monkeypatch.setattr(core, "_WAITING", {me: a, others[0]: b})
    assert core._thread_wait_cycle(me) == [a, b]
    monkeypatch.setattr(core, "_LOCK_OWNERS", {id(a): (others[0], 1), id(b): (others[1], 1)})
    monkeypatch.setattr(core, "_WAITING", {me: a, others[0]: b})
    assert core._thread_wait_cycle(me) is None
