"""Third wave of mutation-driven tests (round 4 survivors, final code)."""

import inspect
import threading
from typing import ForwardRef, Generic, Optional, TypeVar

import pytest

from py_autowired import Container, MissingServiceReplacementError, autowired
from py_autowired import container as core

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


def _wire(monkeypatch, container, consumer):
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(consumer, container, cache.get_raw_map())
    autowired._patch_init(consumer, container, cache.get_raw_map())
    return cache


# ---- field injection loop must continue past unmatched fields ---------------
def test_unmatched_field_does_not_stop_injection_of_later_fields(monkeypatch):
    service = type("Zeta", (), {"__module__": "feature.services"})
    consumer = type("Consumer", (), {
        "__module__": "feature.consumer",
        "alpha_instance": None,      # sorted first, no registration
        "zeta_instance": None,       # sorted later, registered
    })
    container = Container()
    container.register_transient(service)
    _wire(monkeypatch, container, consumer)
    obj = consumer()
    assert obj.alpha_instance is None and isinstance(obj.zeta_instance, service)


# ---- naming-convention boundaries ------------------------------------------
@pytest.mark.parametrize("class_name", ["ABS_X", "Abs_X", "abs_X", "IX"])
def test_single_letter_conventions(monkeypatch, class_name):
    service = type(class_name, (), {"__module__": "feature.services"})
    container = Container()
    container.register_transient(service)
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "x_instance": None})
    _wire(monkeypatch, container, consumer)
    assert isinstance(consumer().x_instance, service)


def test_class_named_just_i_is_indexed_without_error(monkeypatch):
    service = type("I", (), {"__module__": "feature.services"})
    container = Container()
    container.register_transient(service)
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "i_instance": None})
    _wire(monkeypatch, container, consumer)
    assert isinstance(consumer().i_instance, service)


# ---- _parameter_hint with a ForwardRef raw annotation -----------------------
class Dep:
    pass


def test_forward_ref_raw_annotation_is_not_treated_as_explicit():
    parameter = inspect.Parameter("d", inspect.Parameter.KEYWORD_ONLY, default=None,
                                  annotation=ForwardRef("Dep"))
    assert core._parameter_hint(parameter, {"d": Optional[Dep]}) is Dep


# ---- constructor parameter loop --------------------------------------------
class Mixed:
    def __init__(self, label="plain", dep: Dep = None):
        self.label, self.dep = label, dep


class TypeVarDefault:
    def __init__(self, value: T = 5):
        self.value = value


def test_unannotated_default_does_not_stop_later_injection_and_typevar_keeps_default():
    container = Container()
    container.register_transient(Dep)
    container.register_transient(Mixed)
    container.register_transient(TypeVarDefault)
    mixed = container.resolve(Mixed)
    assert mixed.label == "plain" and isinstance(mixed.dep, Dep)
    assert container.resolve(TypeVarDefault).value == 5


# ---- _accepts_no_arguments --------------------------------------------------
def test_accepts_no_arguments_rules():
    class VarOnly:
        def __init__(self, *args, **kwargs): ...

    class VarThenRequired:
        def __init__(self, *args, required): ...

    class Defaults:
        def __init__(self, a=1, *, b=2): ...

    class Required:
        def __init__(self, a): ...

    assert core._accepts_no_arguments(VarOnly) is True
    assert core._accepts_no_arguments(VarThenRequired) is False
    assert core._accepts_no_arguments(Defaults) is True
    assert core._accepts_no_arguments(Required) is False
    assert core._accepts_no_arguments(int) is True       # signature not inspectable


# ---- open generic with mismatching parameter counts ------------------------
class Box(Generic[T]):
    pass


class PairImpl(Box[K], Generic[K, V]):
    pass


def test_typevar_map_only_when_parameter_counts_match():
    container = Container()
    container.register_open_generic(Box, PairImpl)
    registration = container._closed_generic_registration(Box[int], cache=False)
    assert registration.typevar_map is None


# ---- _store_registration's own guards (race path) ---------------------------
def test_store_registration_policies_directly():
    container = Container(strict_registrations=True)
    reg = container._registration(Dep, None, core.LifetimeScope.TRANSIENT)
    assert container._store_registration(reg) is True
    again = container._registration(Dep, None, core.LifetimeScope.TRANSIENT)
    assert container._store_registration(again, policy="fallback") is False
    assert container.registrations[Dep] is reg
    assert container._store_registration(again, policy="replace") is True   # strict allows replace
    assert container.registrations[Dep] is again
    missing = container._registration(Mixed, None, core.LifetimeScope.TRANSIENT)
    with pytest.raises(MissingServiceReplacementError):
        container._store_registration(missing, policy="replace")
    assert container._store_registration(missing, policy="fallback") is True


# ---- cache retention and ownership bookkeeping ------------------------------
def test_lookup_is_cached_when_no_refresh_happened(monkeypatch):
    service = type("Clock", (), {"__module__": "feature.clock"})
    container = Container()
    container.register_transient(service)
    cache = autowired._CachedNameMap(container)
    first = cache.get_cached_registration("clock", "feature.consumer")
    assert cache._resolution_cache[("clock", "feature.consumer")] is first
    assert cache.get_cached_registration("clock", "feature.consumer") is first


def test_ownership_depth_three_and_foreign_owner(monkeypatch):
    container = Container()
    container.register_singleton(Dep)
    registration = container.registrations[Dep]
    me = threading.get_ident()
    for _ in range(3):
        container._acquire_creation(registration)
    assert core._LOCK_OWNERS[id(registration)] == (me, 3)
    container._release_creation(registration)
    assert core._LOCK_OWNERS[id(registration)] == (me, 2)
    container._release_creation(registration)
    container._release_creation(registration)
    assert id(registration) not in core._LOCK_OWNERS

    owners = {}
    monkeypatch.setattr(core, "_LOCK_OWNERS", owners)
    foreign = me + 1
    owners[id(registration)] = (foreign, 1)
    core.Container._note_owner(registration, me)
    assert owners[id(registration)] == (me, 1)          # a foreign entry is replaced, not extended
    owners[id(registration)] = (foreign, 2)
    registration.lock.acquire()
    core.Container._release_creation(registration)
    assert owners[id(registration)] == (foreign, 2)     # never touch another thread's entry
