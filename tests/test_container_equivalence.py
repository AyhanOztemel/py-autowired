"""Container behaviours that let a large app drop its own container copy.

Every test also asserts that the 0.3.0 default is unchanged, because the
package must stay a drop-in upgrade for existing users.
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path

import pytest

from py_autowired.autowired import auto_inject
from py_autowired.container import (
    AmbiguousImplementationError,
    Container,
    DependencyInjectionError,
    LifetimeScope,
    ResolutionError,
    ScopeNotActiveError,
    _accepts_no_arguments,
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


# --- field injection marker --------------------------------------------------


class FieldOnlyUser:
    __di_attrs__ = ("clock_instance",)

    def __init__(self, clock: SystemClock | None = None):
        self.clock = clock
        self.clock_instance = None


class MixedUser:
    """Uses field injection *and* a required constructor dependency."""

    __di_attrs__ = ("clock_instance",)

    def __init__(self, clock: SystemClock):
        self.clock = clock
        self.clock_instance = None


def test_field_only_classes_skip_constructor_injection():
    container = Container()
    container.register_singleton(SystemClock)
    container.register(FieldOnlyUser)
    assert container.resolve(FieldOnlyUser).clock is None


def test_classes_needing_constructor_arguments_still_get_them():
    container = Container()
    container.register_singleton(SystemClock)
    container.register(MixedUser)
    assert container.resolve(MixedUser).clock.now() == "system"


def test_the_constructor_check_is_inspected_only_once_per_class():
    container = Container()
    container.register_singleton(SystemClock)
    container.register(FieldOnlyUser, scope=LifetimeScope.TRANSIENT)

    container.resolve(FieldOnlyUser)
    before = _accepts_no_arguments.cache_info()
    for _ in range(50):
        container.resolve(FieldOnlyUser)
    after = _accepts_no_arguments.cache_info()

    assert after.misses == before.misses
    assert after.hits == before.hits + 50


def test_classes_without_the_marker_are_unaffected():
    class PlainUser:
        def __init__(self, clock: SystemClock):
            self.clock = clock

    container = Container()
    container.register_singleton(SystemClock)
    container.register(PlainUser)
    assert container.resolve(PlainUser).clock.now() == "system"


def test_auto_inject_fills_the_field_end_to_end(tmp_path: Path):
    """The real path: auto_inject discovers and patches, the container builds."""

    module_name = "e2e_report_module"
    (tmp_path / f"{module_name}.py").write_text(
        "class Report:\n"
        "    def __init__(self):\n"
        "        self.clock_instance = None\n"
        "\n"
        "    def stamp(self):\n"
        "        return self.clock_instance.now()\n",
        encoding="utf-8",
    )

    container = Container()
    container.register_singleton(ABS_Clock, SystemClock)

    sys.path.insert(0, str(tmp_path))
    try:
        assert auto_inject(container, root_dir=str(tmp_path)) >= 1
        Report = sys.modules[module_name].Report
        assert Report.__di_attrs__ == ("clock_instance",)

        container.register(Report)
        built = container.resolve(Report)
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(tmp_path))

    assert built.clock_instance is not None
    assert built.stamp() == "system"


# --- implementation selector -------------------------------------------------


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


def test_selectors_are_per_container():
    configured = Container()
    configured.set_implementation_selector(ABS_Clock, lambda: FrozenClock)
    plain = Container()
    with pytest.raises(AmbiguousImplementationError):
        plain.register(ABS_Clock)


# --- unchanged 0.3.0 surface -------------------------------------------------


class ABS_Unimplemented(ABC):
    @abstractmethod
    def run(self) -> None: ...


def test_scoped_resolution_without_a_scope_fails():
    container = Container()
    container.register_scoped(Session)
    with pytest.raises(ScopeNotActiveError):
        container.resolve(Session)


def test_nested_scoped_functions_stay_isolated():
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


def test_resolution_error_keeps_its_only_base_class():
    assert issubclass(ResolutionError, DependencyInjectionError)
    assert not issubclass(ResolutionError, ValueError)
    with pytest.raises(ResolutionError):
        Container().register(ABS_Unimplemented)


def test_the_constructor_takes_no_new_settings():
    container = Container()
    assert container.strict_interfaces is False
    assert container.strict_registrations is False
    with pytest.raises(TypeError):
        Container(ambient_scope=True)
    with pytest.raises(TypeError):
        Container(abstract_detection="loose")
