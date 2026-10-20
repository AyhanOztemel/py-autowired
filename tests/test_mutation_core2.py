"""Second wave of mutation-driven tests (core survivors of round 3)."""

import abc
import inspect
import threading
from typing import Annotated, Generic, Optional, TypeVar, Union

import pytest

from py_autowired import (
    AmbiguousImplementationError, Container, Keyed, Lazy, Provider, ResolutionError,
    ServiceNotRegisteredError, autowired, refresh,
)
from py_autowired import container as core

T = TypeVar("T")


# ---- _parameter_hint: version-independent hints (exercised on every Python) ----
def _param(name, annotation, default):
    return inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)


class Dep:
    pass


@pytest.mark.parametrize("raw", [Dep, "Dep"])
def test_implicit_optional_from_none_default_is_dropped(raw):
    hint = core._parameter_hint(_param("d", raw, None), {"d": Optional[Dep]})
    assert hint is Dep


@pytest.mark.parametrize("raw", [Optional[Dep], Union[Dep, None], "Optional[Dep]", "Dep | None", "Union[Dep, None]"])
def test_explicit_optional_is_kept(raw):
    hint = core._parameter_hint(_param("d", raw, None), {"d": Optional[Dep]})
    assert hint == Optional[Dep]


def test_non_none_default_and_wider_unions_are_untouched():
    assert core._parameter_hint(_param("d", Dep, 5), {"d": Optional[Dep]}) == Optional[Dep]
    wide = Union[Dep, int, None]
    assert core._parameter_hint(_param("d", Dep, None), {"d": wide}) == wide
    assert core._parameter_hint(_param("d", Dep, None), {}) is Dep        # no hint: raw annotation
    assert core._parameter_hint(_param("d", Dep, None), {"d": Dep}) is Dep


def test_none_first_union_order_is_handled():
    hint = core._parameter_hint(_param("d", Dep, None), {"d": Union[None, Dep]})
    assert hint is Dep


# ---- cross-thread bookkeeping -----------------------------------------------
def test_reentrant_creation_lock_tracks_depth_and_cleans_up():
    container = Container()
    container.register_singleton(Dep)
    registration = container.registrations[Dep]
    me = threading.get_ident()
    container._acquire_creation(registration)
    container._acquire_creation(registration)
    assert core._LOCK_OWNERS[id(registration)] == (me, 2)
    container._release_creation(registration)
    assert core._LOCK_OWNERS[id(registration)] == (me, 1)
    container._release_creation(registration)
    assert id(registration) not in core._LOCK_OWNERS
    assert registration.lock.acquire(blocking=False)                 # really released
    registration.lock.release()


def test_wait_cycle_detection_is_exact(monkeypatch):
    a, b, c = object(), object(), object()
    me, t2, t3 = 1, 2, 3
    owners = {id(a): (t2, 1), id(b): (me, 1), id(c): (t3, 1)}
    monkeypatch.setattr(core, "_LOCK_OWNERS", owners)
    monkeypatch.setattr(core, "_WAITING", {me: a, t2: b})
    assert core._thread_wait_cycle(me) == [a, b]                     # me->a(t2)->b(me)
    monkeypatch.setattr(core, "_WAITING", {me: a})
    assert core._thread_wait_cycle(me) is None                       # t2 is not waiting
    monkeypatch.setattr(core, "_WAITING", {me: c, t3: a, t2: c})
    assert core._thread_wait_cycle(me) is None                       # t3<->t2 loop excludes me
    monkeypatch.setattr(core, "_WAITING", {})
    assert core._thread_wait_cycle(me) is None
    monkeypatch.setattr(core, "_WAITING", {me: a})
    monkeypatch.setattr(core, "_LOCK_OWNERS", {})
    assert core._thread_wait_cycle(me) is None                       # owner unknown


# ---- keyed / lazy / provider details --------------------------------------
class IClock:
    pass


class UtcClock(IClock):
    pass


class Report:
    def __init__(self, clock: Annotated[IClock, Keyed("utc")], dep: Dep,
                 tz: Annotated[IClock, Keyed("tz")] = None):
        self.clock, self.dep, self.tz = clock, dep, tz


class NeedsMissingKey:
    def __init__(self, clock: Annotated[IClock, Keyed("nope")]):
        self.clock = clock


def test_keyed_parameter_then_normal_parameter_and_keyed_default():
    container = Container()
    container.register_keyed(IClock, "utc", UtcClock)
    container.register_transient(Dep)
    container.register_transient(Report)
    report = container.resolve(Report)
    assert isinstance(report.clock, UtcClock) and isinstance(report.dep, Dep) and report.tz is None
    container.register_transient(NeedsMissingKey)
    with pytest.raises(ServiceNotRegisteredError, match="keyed"):
        container.resolve(NeedsMissingKey)
    assert container.is_keyed_registered(IClock, "utc") and not container.is_keyed_registered(IClock, "x")


def test_keyed_marker_equality_and_lazy_state():
    assert Keyed("a") == Keyed("a") and Keyed("a") != Keyed("b") and Keyed("a") != "a"
    assert hash(Keyed("a")) == hash(Keyed("a")) and repr(Keyed("a")) == "Keyed('a')"
    container = Container()
    container.register_transient(Dep)
    lazy = Lazy(container, Dep)
    assert lazy.is_value_created is False
    value = lazy.value
    assert lazy.is_value_created is True and lazy.value is value
    assert repr(lazy) == "Lazy[Dep]" and repr(Provider(container, Dep)) == "Provider[Dep]"


# ---- open generics / implementation selection / refresh / policies ----------
class Box(Generic[T]):
    pass


class PlainBox(Box[int]):          # no TypeVar parameters left
    pass


class Pair(Generic[T]):
    def __init__(self, item: T):
        self.item = item


def test_closed_generic_details():
    container = Container()
    assert container._closed_generic_registration(Box[int]) is None        # not registered
    assert container._closed_generic_registration(Dep) is None             # not generic
    container.register_open_generic(Box, PlainBox)
    probe = container._closed_generic_registration(Box[int], cache=False)
    assert probe.typevar_map is None and Box[int] not in container._closed_generics
    assert isinstance(container.resolve(Box[int]), PlainBox)
    assert Box[int] in container._closed_generics
    container.register_transient(Dep)
    container.register_open_generic(Pair)
    assert isinstance(container.resolve(Pair[Dep]).item, Dep)


class Shape(abc.ABC):
    @abc.abstractmethod
    def area(self): ...


class Square(Shape):
    def area(self):
        return 1


class Abstract2(abc.ABC):
    @abc.abstractmethod
    def f(self): ...


class ImplA(Abstract2):
    def f(self): ...


class ImplB(Abstract2):
    def f(self): ...


class Lonely(abc.ABC):
    @abc.abstractmethod
    def g(self): ...


def test_abstract_registration_picks_single_implementation_or_fails():
    container = Container()
    container.register_transient(Shape)
    assert type(container.resolve(Shape)) is Square
    with pytest.raises(AmbiguousImplementationError):
        container.register_transient(Abstract2)
    container.register_transient(Lonely)               # 0.5.1: looked up when first resolved
    with pytest.raises(ResolutionError):
        container.resolve(Lonely)


def test_refresh_reports_whether_a_cache_exists(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    assert refresh() is False
    container = Container()
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    before = cache._generation
    assert refresh() is True and cache._generation != before


def test_fallback_registration_returns_false_and_keeps_original():
    container = Container()
    assert container.register_fallback(Dep) is True
    assert container.register_fallback(Dep, Dep) is False
    assert container.registration_history == [("fallback", Dep), ("fallback-skipped", Dep)]
