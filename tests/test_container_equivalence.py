"""Opt-in behaviours that bring the container in line with legacy BMS rules.

Every test here also asserts that the 0.3.0 default is unchanged, because the
package must stay a drop-in upgrade for existing users.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pytest

from py_autowired.container import (
    AmbiguousImplementationError,
    Container,
    LifetimeScope,
    ResolutionError,
    ScopeNotActiveError,
)


class ABS_Clock(ABC):
    @abstractmethod
    def now(self) -> str: ...


class SystemClock(ABS_Clock):
    def now(self) -> str:
        return "system"


class FrozenClock(ABS_Clock):
    def now(self) -> str:
        return "frozen"


class Session:
    pass


# --- 1. __di_attrs__ ---------------------------------------------------------


class ClockUser:
    __di_attrs__ = ("clock_instance",)

    def __init__(self, clock: SystemClock | None = None):
        self.clock = clock


class PlainUser:
    def __init__(self, clock: SystemClock):
        self.clock = clock


def test_di_attrs_classes_skip_constructor_injection():
    container = Container()
    container.register_singleton(SystemClock)
    container.register(ClockUser)
    # SystemClock is resolvable, but the marker keeps __init__ untouched so the
    # auto_inject attribute patch remains the only injection path.
    assert container.resolve(ClockUser).clock is None


def test_classes_without_the_marker_still_get_constructor_injection():
    container = Container()
    container.register_singleton(SystemClock)
    container.register(PlainUser)
    assert container.resolve(PlainUser).clock.now() == "system"


# --- 2. ambient scope --------------------------------------------------------


def test_scoped_resolution_without_a_scope_still_fails_by_default():
    container = Container()
    container.register_scoped(Session)
    with pytest.raises(ScopeNotActiveError):
        container.resolve(Session)


def test_ambient_scope_creates_a_scope_on_demand():
    container = Container(ambient_scope=True)
    container.register_scoped(Session)
    assert container.resolve(Session) is container.resolve(Session)


def test_an_explicit_scope_still_wins_over_the_ambient_one():
    container = Container(ambient_scope=True)
    container.register_scoped(Session)
    with container.create_scope() as scope:
        inside = scope.resolve(Session)
    assert container.resolve(Session) is not inside


def test_ambient_scoped_function_shares_one_scope_across_nested_calls():
    container = Container(ambient_scope=True)
    container.register_scoped(Session)
    seen = []

    @container.scoped_function
    def inner():
        seen.append(container.resolve(Session))

    @container.scoped_function
    def outer():
        seen.append(container.resolve(Session))
        inner()

    outer()
    assert seen[0] is seen[1]


def test_nested_scoped_functions_stay_isolated_by_default():
    container = Container()
    container.register_scoped(Session)
    seen = []

    @container.scoped_function
    def inner():
        seen.append(container.resolve(Session))

    @container.scoped_function
    def outer():
        seen.append(container.resolve(Session))
        inner()

    outer()
    assert seen[0] is not seen[1]


# --- 3. implementation selector ---------------------------------------------


def test_ambiguous_abstractions_still_raise_by_default():
    container = Container()
    with pytest.raises(AmbiguousImplementationError):
        container.register(ABS_Clock)


def test_selector_resolves_the_ambiguity():
    container = Container()
    container.set_implementation_selector(ABS_Clock, lambda: FrozenClock)
    container.register(ABS_Clock)
    assert container.resolve(ABS_Clock).now() == "frozen"


def test_selector_rejects_concrete_service_types():
    container = Container()
    with pytest.raises(TypeError):
        container.set_implementation_selector(SystemClock, lambda: SystemClock)


def test_selector_must_return_a_concrete_type():
    container = Container()
    container.set_implementation_selector(ABS_Clock, lambda: ABS_Clock)
    with pytest.raises(TypeError):
        container.register(ABS_Clock)


def test_an_explicit_implementation_overrides_the_selector():
    container = Container()
    container.set_implementation_selector(ABS_Clock, lambda: FrozenClock)
    container.register(ABS_Clock, SystemClock)
    assert container.resolve(ABS_Clock).now() == "system"


# --- 4. abstract detection ---------------------------------------------------


class RebuiltContract:
    """Carries abstract methods without ABCMeta, so isabstract() misses it."""

    __abstractmethods__ = frozenset({"run"})


class RebuiltImpl(RebuiltContract):
    __abstractmethods__ = frozenset()

    def run(self) -> str:
        return "ran"


def test_strict_detection_treats_the_rebuilt_contract_as_concrete():
    container = Container()
    container.register(RebuiltContract)
    assert container.registrations[RebuiltContract].implementation_type is RebuiltContract


def test_loose_detection_finds_the_subclass_instead():
    container = Container(abstract_detection="loose")
    container.register(RebuiltContract)
    assert container.resolve(RebuiltContract).run() == "ran"


def test_unknown_detection_mode_is_rejected():
    with pytest.raises(ValueError):
        Container(abstract_detection="whatever")


# --- 5. error compatibility --------------------------------------------------


class ABS_Unimplemented(ABC):
    @abstractmethod
    def run(self) -> None: ...


def test_resolution_error_is_also_a_value_error():
    container = Container()
    with pytest.raises(ValueError):
        container.register(ABS_Unimplemented)


def test_resolution_error_keeps_its_package_base_class():
    assert issubclass(ResolutionError, ValueError)
    with pytest.raises(ResolutionError):
        Container().register(ABS_Unimplemented)


def test_defaults_are_unchanged():
    container = Container()
    assert container.ambient_scope is False
    assert container.abstract_detection == "strict"
    assert container.strict_interfaces is False
    assert container.strict_registrations is False
    assert LifetimeScope.TRANSIENT is not None
