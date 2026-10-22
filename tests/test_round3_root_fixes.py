"""0.5.4, root fixes after the second blind review (A1-A6): each one changes behaviour, so
each is pinned by a test that failed on the code before it."""

import asyncio
import functools
import inspect
import logging
import sys
import textwrap
import types
from typing import Annotated

import pytest

from typing import Callable, Optional, Union, get_args, get_type_hints

from py_autowired import (
    AutowiringValidationError,
    Container,
    Keyed,
    Lazy,
    ServiceNotRegisteredError,
    autowired,
    validate_autowiring,
)
from py_autowired import container as container_module


def _call(function, *args):
    try:
        function(*args)
    except Exception as error:  # noqa: BLE001 - the test inspects it
        return error
    return None


def _warnings(caplog, *parts):
    return [r.getMessage() for r in list(caplog.records)
            if r.levelno == logging.WARNING and all(p in r.getMessage() for p in parts)]


@pytest.fixture
def graph(monkeypatch):
    container = Container()
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)

    def patch(*classes):
        for cls in classes:
            autowired._patch_setattr(cls, container, cache.get_raw_map())
            autowired._patch_init(cls, container, cache.get_raw_map())

    return container, patch


def _consumer(*field_names):
    return type("ConsumerA", (), {"__module__": __name__, **{name: None for name in field_names}})


# ---- A1: positional-only constructor parameters are supported ------------------------------

class _DepA1:
    pass


class _UnregisteredA1:
    pass


class _PositionalA1:
    def __init__(self, dep: _DepA1, /, *, flag: bool = True):
        self.dep, self.flag = dep, flag


class _GapA1:
    """A skipped positional-only parameter before a resolved one keeps its default."""

    def __init__(self, first: _UnregisteredA1 = "kept", second: _DepA1 = None, /):
        self.first, self.second = first, second


class _KeyedPositionalA1:
    def __init__(self, dep: Annotated[_DepA1, Keyed("k")], /):
        self.dep = dep


def test_a1_a_positional_only_dependency_is_injected():
    container = Container()
    container.register_transient(_DepA1)
    container.register_transient(_PositionalA1)
    error = _call(container.resolve, _PositionalA1)
    assert error is None, f"positional-only parameter not supported: {error!r}"
    made = container.resolve(_PositionalA1)
    assert isinstance(made.dep, _DepA1) and made.flag is True


def test_a1_a_skipped_positional_parameter_keeps_its_default_before_a_resolved_one():
    container = Container()
    container.register_transient(_DepA1)
    container.register_transient(_GapA1)
    error = _call(container.resolve, _GapA1)
    assert error is None, f"positional-only parameter not supported: {error!r}"
    made = container.resolve(_GapA1)
    assert made.first == "kept" and isinstance(made.second, _DepA1)


def test_a1_a_keyed_positional_only_dependency_is_injected():
    container = Container()
    container.register_keyed(_DepA1, "k", _DepA1)
    container.register_transient(_KeyedPositionalA1)
    error = _call(container.resolve, _KeyedPositionalA1)
    assert error is None, f"positional-only parameter not supported: {error!r}"


def test_a1_validation_accepts_what_the_container_builds(graph):
    container, patch = graph
    container.register_transient(_DepA1)
    container.register_keyed(_DepA1, "k", _DepA1)
    for cls in (_PositionalA1, _GapA1, _KeyedPositionalA1):
        container.register_transient(cls)
    patch(_consumer("positionala1_instance", "gapa1_instance", "keyedpositionala1_instance"))
    error = _call(validate_autowiring, container)
    assert error is None, getattr(error, "issues", error)


# ---- A2: type hints are read once, with their metadata ------------------------------------

class _DepA2:
    pass


class _KeyedByStringA2:
    def __init__(self, dep: "Annotated[_DepA2, Keyed('x')]"):
        self.dep = dep


class _PlainA2:
    def __init__(self, dep: _DepA2):
        self.dep = dep


def _extras_fail(monkeypatch):
    real = container_module.get_type_hints

    def flaky(obj, *args, **kwargs):
        if kwargs.get("include_extras"):
            raise NameError("extras failed")
        return real(obj, *args, **kwargs)

    monkeypatch.setattr(container_module, "get_type_hints", flaky)


def test_a2_lost_keyed_metadata_never_selects_the_unkeyed_registration(monkeypatch):
    _extras_fail(monkeypatch)
    container = Container()
    container.register_transient(_DepA2)                      # the wrong (unkeyed) candidate
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(_KeyedByStringA2)
    error = _call(container.resolve, _KeyedByStringA2)
    assert isinstance(error, ServiceNotRegisteredError), f"unkeyed registration used: {error!r}"


def test_a2_a_constructor_without_metadata_reads_its_hints_once(monkeypatch):
    calls = []
    real = container_module.get_type_hints

    def counting(obj, *args, **kwargs):
        if obj is _PlainA2.__init__:
            calls.append(kwargs.get("include_extras", False))
        return real(obj, *args, **kwargs)

    monkeypatch.setattr(container_module, "get_type_hints", counting)
    container = Container()
    container.register_transient(_DepA2)
    container.register_transient(_PlainA2)
    assert isinstance(container.resolve(_PlainA2).dep, _DepA2)
    assert len(calls) == 1, calls


def test_a2_validation_does_not_select_the_unkeyed_registration_either(monkeypatch, graph):
    _extras_fail(monkeypatch)
    container, patch = graph
    container.register_transient(_DepA2)
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(_KeyedByStringA2)
    patch(_consumer("keyedbystringa2_instance"))
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError), "validation passed an unkeyed resolution"


def _top(self, a: Annotated[_DepA2, Keyed("x")], b: Optional[_DepA2] = None): ...
def _nested(self, a: Lazy[Annotated[_DepA2, Keyed("x")]]): ...
def _in_callable(self, a: Callable[[Annotated[_DepA2, Keyed("x")]], int]): ...
def _no_metadata(self, a: _DepA2, b: Lazy[_DepA2]): ...


@pytest.mark.parametrize("function", [_top, _nested, _in_callable, _no_metadata])
def test_a2_the_plain_view_is_exactly_get_type_hints(function):
    hints, extras, error = container_module._constructor_hints(function)
    assert error is None
    assert hints == get_type_hints(function)
    assert extras == get_type_hints(function, include_extras=True)


def test_a2_a_plain_pass_failing_after_the_metadata_pass_keeps_the_metadata(monkeypatch):
    real = container_module.get_type_hints

    def inverse(obj, *args, **kwargs):
        if not kwargs.get("include_extras"):
            raise NameError("plain failed")
        return real(obj, *args, **kwargs)

    monkeypatch.setattr(container_module, "get_type_hints", inverse)
    hints, extras, error = container_module._constructor_hints(_top)
    assert hints == {} and isinstance(error, NameError)
    assert extras["a"] == Annotated[_DepA2, Keyed("x")]


# ---- A3: injected() fills the fields of the ONE object it builds; the class is not touched ---

class _ClockA3:
    pass


def _request_for(container):
    state = type("State", (), {"py_autowired_container": container})()
    app = type("App", (), {"state": state})()
    return type("Request", (), {"app": app})()


def _counting_scans(monkeypatch):
    scans = []
    real_scan = autowired._find_di_candidates
    monkeypatch.setattr(autowired, "_find_di_candidates", lambda cls: scans.append(cls) or real_scan(cls))
    return scans


def test_a3_injected_fills_the_fields_of_an_unpatched_class(graph, monkeypatch):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    container, _patch = graph
    container.register_singleton(_ClockA3)

    class NeverPatchedA3:
        def __init__(self):
            self.clocka3_instance = None

    autowired._NAME_MAP_CACHE.get_raw_map()                  # the cache builds its own map first
    maps = []
    real_map = autowired._build_name_map_from_container
    monkeypatch.setattr(autowired, "_build_name_map_from_container",
                        lambda owner: maps.append(owner) or real_map(owner))
    scans = _counting_scans(monkeypatch)
    made = asyncio.run(injected(NeverPatchedA3)(_request_for(container)))
    assert isinstance(made.clocka3_instance, _ClockA3)
    again = asyncio.run(injected(NeverPatchedA3)(_request_for(container)))
    assert again is not made and again.clocka3_instance is made.clocka3_instance
    assert scans.count(NeverPatchedA3) == 1                  # the field list is kept per class
    assert maps == []                                         # the active cache is used


def test_a3_without_an_active_container_the_field_stays_none_with_a_warning(monkeypatch, caplog):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)  # the application's startup has not run
    container = Container()
    container.register_singleton(_ClockA3)

    class NoCacheA3:
        def __init__(self):
            self.clocka3_instance = None

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        made = asyncio.run(injected(NoCacheA3)(_request_for(container)))
    assert made.clocka3_instance is None                     # as in 0.5.3
    assert autowired._NAME_MAP_CACHE is None                 # nothing was made active
    assert _warnings(caplog, "NoCacheA3.clocka3_instance", "startup has not run")


def test_a3_a_patched_class_is_not_scanned_again(graph, monkeypatch):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    container, patch = graph
    container.register_singleton(_ClockA3)

    class PatchedA3:
        def __init__(self):
            self.clocka3_instance = None

    patch(PatchedA3)
    scans = _counting_scans(monkeypatch)
    made = asyncio.run(injected(PatchedA3)(_request_for(container)))
    assert isinstance(made.clocka3_instance, _ClockA3)
    assert scans == []


def test_a3_the_class_itself_is_not_changed(graph):
    """Control: modules outside the project are never touched - the same class built
    anywhere else gets no injection."""
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    container, _patch = graph
    container.register_singleton(_ClockA3)

    class OutsideA3:
        def __init__(self):
            self.clocka3_instance = None

    asyncio.run(injected(OutsideA3)(_request_for(container)))
    assert "__ai_ioc_init__" not in OutsideA3.__dict__
    assert "__setattr__" not in OutsideA3.__dict__
    assert OutsideA3().clocka3_instance is None


def test_a3_a_field_with_no_registration_is_a_warning(graph, caplog):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    container, _patch = graph

    class MissingA3:
        def __init__(self):
            self.nothingregistereda3_instance = None

    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        made = asyncio.run(injected(MissingA3)(_request_for(container)))
        asyncio.run(injected(MissingA3)(_request_for(container)))
    assert made.nothingregistereda3_instance is None
    assert len(_warnings(caplog, "MissingA3.nothingregistereda3_instance", "no matching registration")) == 1


def test_a3_a_value_set_by_the_constructor_is_kept(graph):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    container, _patch = graph
    container.register_singleton(_ClockA3)
    own = _ClockA3()

    class OwnClockA3:
        def __init__(self):
            self.clocka3_instance = own

    assert asyncio.run(injected(OwnClockA3)(_request_for(container))).clocka3_instance is own


def test_a3_a_class_without_fields_is_scanned_once(graph, monkeypatch):
    pytest.importorskip("fastapi")
    from py_autowired.fastapi import injected

    container, _patch = graph
    scans = _counting_scans(monkeypatch)

    class PlainControllerA3:
        pass

    asyncio.run(injected(PlainControllerA3)(_request_for(container)))
    asyncio.run(injected(PlainControllerA3)(_request_for(container)))
    assert scans.count(PlainControllerA3) == 1


# ---- A4: a broken __signature__ is rebuilt from the function's code ------------------------

class _DepA4:
    pass


def _opaque_init(self, dep: _DepA4):
    self.dep = dep


_opaque_init.__signature__ = "not a signature"


class _OpaqueA4:
    __init__ = _opaque_init


def _broken_wrapper(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        return function(*args, **kwargs)

    wrapper.__signature__ = "broken"
    return wrapper


class _WrappedA4:
    @_broken_wrapper
    def __init__(self, dep: _DepA4):
        self.dep = dep


def _required_init(self, dependency):
    self.dependency = dependency


_required_init.__signature__ = "not a signature"


class _OpaqueFieldA4:
    __init__ = _required_init
    clock_instance = None


def _opaque_defaults_init(self, dep: _DepA4, extra: int = 7, *, flag: bool = False):
    self.dep, self.extra, self.flag = dep, extra, flag


_opaque_defaults_init.__signature__ = "not a signature"


class _OpaqueDefaultsA4:
    __init__ = _opaque_defaults_init


def _opaque_hint_init(self, dep: "UndefinedNameA4"):  # noqa: F821
    self.dep = dep


_opaque_hint_init.__signature__ = "not a signature"


class _OpaqueHintA4:
    __init__ = _opaque_hint_init


class _CallableInit:
    __signature__ = "broken"

    def __call__(self, *args, **kwargs):
        pass


class _CodelessA4:
    __init__ = _CallableInit()


@pytest.mark.parametrize("cls", [_OpaqueA4, _WrappedA4])
def test_a4_a_broken_signature_is_rebuilt_from_the_code(cls, caplog):
    container = Container()
    container.register_transient(_DepA4)
    container.register_transient(cls)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        error = _call(container.resolve, cls)
        assert error is None, f"signature not rebuilt: {error!r}"
        assert isinstance(container.resolve(cls).dep, _DepA4)
    assert len(_warnings(caplog, cls.__name__, "rebuilt from its code")) == 1


def test_a4_a_rebuilt_signature_keeps_defaults_and_keyword_defaults():
    container = Container()
    container.register_transient(_DepA4)
    container.register_transient(_OpaqueDefaultsA4)
    made = container.resolve(_OpaqueDefaultsA4)
    assert isinstance(made.dep, _DepA4) and made.extra == 7 and made.flag is False


def test_a4_a_rebuilt_signature_keeps_its_annotations(graph):
    container, patch = graph
    container.register_transient(_OpaqueHintA4)
    patch(_consumer("opaquehinta4_instance"))
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError)
    assert any("_OpaqueHintA4.dep: type hint cannot be resolved" in issue for issue in error.issues)


def test_a4_the_original_error_is_reported_when_nothing_can_be_rebuilt(monkeypatch):
    calls = []

    def broken(function, *args, **kwargs):
        calls.append(function)
        raise ValueError(f"call {len(calls)}")

    monkeypatch.setattr(container_module, "inspect",
                        types.SimpleNamespace(signature=broken, unwrap=inspect.unwrap))
    with pytest.raises(ValueError, match="call 1"):
        container_module._init_signature(_PositionalA1)
    assert len(calls) == 2                                    # the original, then the rebuilt copy


def test_a4_a_wrapper_loop_reports_the_original_error():
    def looping(self, dep):
        pass

    looping.__signature__ = "broken"
    looping.__wrapped__ = looping                              # inspect.unwrap raises ValueError
    cls = type("LoopA4", (), {"__init__": looping})
    with pytest.raises(TypeError):
        container_module._init_signature(cls)


def test_a4_a_field_class_with_a_rebuilt_signature_is_validated(graph):
    container, patch = graph
    container.register_singleton(_OpaqueFieldA4)
    _OpaqueFieldA4.__di_attrs__ = ("clock_instance",)
    try:
        patch(_consumer("opaquefielda4_instance"))
        error = _call(validate_autowiring, container)
        assert isinstance(error, AutowiringValidationError), "a required parameter was not seen"
        assert any("_OpaqueFieldA4.dependency" in issue for issue in error.issues)
        assert "needs a type annotation or default" in str(_call(container.resolve, _OpaqueFieldA4))
    finally:
        del _OpaqueFieldA4.__di_attrs__


def test_a4_a_callable_without_code_stays_unknown(caplog):
    """Guard: nothing can be rebuilt without a code object; the old path is kept."""
    with pytest.raises((TypeError, ValueError)):
        container_module._init_signature(_CodelessA4)
    assert container_module._accepts_no_arguments(_CodelessA4) is True


def test_k7_a_lifetime_only_follow_never_reports_an_unreadable_signature(graph):
    """Through Lazy only lifetime problems are followed: an unreadable signature is not one."""
    container, patch = graph
    container.register_transient(_CodelessA4)
    holder = type("LazyCodelessHolder", (), {"__module__": __name__})

    def __init__(self, inner: Lazy[_CodelessA4]):
        self.inner = inner

    holder.__init__ = __init__
    container.register_singleton(holder)
    patch(_consumer("lazycodelessholder_instance"))
    issues = getattr(_call(validate_autowiring, container), "issues", ())
    assert [issue for issue in issues if "cannot be read" in issue] == []


def test_a4_an_ordinary_signature_is_python_s_own():
    """Guard: the helper returns exactly what inspect.signature returns."""
    assert container_module._init_signature(_PositionalA1) == inspect.signature(_PositionalA1.__init__)


# ---- A5 (Y16-b): a default that replaces a keyed registration is named ----------------------

class _KeyedDefaultA5:
    def __init__(self, a: "Annotated[_DepA2, Keyed('x')]" = None,
                 b: "UndefinedNameA5" = None,  # noqa: F821
                 c: "Annotated[_DepA2, Keyed('y')]" = None):
        self.a = a


class _KeyedNoDefaultA5:
    def __init__(self, a: "Annotated[_DepA2, Keyed('x')]", b: "UndefinedNameA5" = None):  # noqa: F821
        self.a = a


class _KeyedResolvableA5:
    def __init__(self, a: "Annotated[_DepA2, Keyed('x')]" = None):
        self.a = a


def test_a5_a_default_replacing_a_registered_keyed_dependency_is_named(caplog):
    container = Container()
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_keyed(_DepA2, "y", _DepA2)
    container.register_transient(_KeyedDefaultA5)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        assert container.resolve(_KeyedDefaultA5).a is None                # behaviour unchanged
        container.resolve(_KeyedDefaultA5)
    assert len(_warnings(caplog, "_KeyedDefaultA5.a", "Keyed('x')", "default")) == 1
    assert len(_warnings(caplog, "_KeyedDefaultA5.c", "Keyed('y')", "default")) == 1
    assert _warnings(caplog, "_KeyedDefaultA5.b", "Keyed type hint") == []


def test_a5_no_line_without_a_default_or_when_the_hints_resolve(caplog):
    container = Container()
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(_KeyedNoDefaultA5)
    container.register_transient(_KeyedResolvableA5)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        assert isinstance(_call(container.resolve, _KeyedNoDefaultA5), ServiceNotRegisteredError)
        assert isinstance(container.resolve(_KeyedResolvableA5).a, _DepA2)
    assert _warnings(caplog, "Keyed type hint") == []


# ---- A7: on 3.10 the implicit Optional of "= None" does not hide a Keyed marker ------------

class _KeyedNoneA7:
    def __init__(self, a: Annotated[_DepA2, Keyed("x")] = None):
        self.a = a


class _ScopedA7:
    pass


class _SingletonA7:
    def __init__(self, session: Annotated[_ScopedA7, Keyed("s")] = None):
        self.session = session


def test_a7_a_keyed_dependency_with_a_none_default_is_injected():
    container = Container()
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(_KeyedNoneA7)
    assert isinstance(container.resolve(_KeyedNoneA7).a, _DepA2)


def test_a7_validation_follows_a_keyed_dependency_with_a_none_default(graph):
    from py_autowired import LifetimeScope
    container, patch = graph
    container.register_keyed(_ScopedA7, "s", _ScopedA7, scope=LifetimeScope.SCOPED)
    container.register_singleton(_SingletonA7)
    patch(_consumer("singletona7_instance"))
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError), "the keyed scoped capture was not seen"
    assert any("captures scoped" in issue for issue in error.issues)


# ---- A8: an optional keyed dependency is injected; an unsupported Keyed form is never silent ---

_OPTIONAL_KEYED_FORMS = {
    "optional": Optional[Annotated[_DepA2, Keyed("x")]],
    "union_none": Union[Annotated[_DepA2, Keyed("x")], None],
    "pipe_none": Annotated[_DepA2, Keyed("x")] | None,
    "optional_inside": Annotated[Optional[_DepA2], Keyed("x")],
}


def _class_with(annotation, default=None, name="FormA8"):
    def __init__(self, a=default):
        self.a = a

    __init__.__annotations__ = {"a": annotation}
    return type(name, (), {"__init__": __init__, "__module__": __name__})


@pytest.mark.parametrize("form", list(_OPTIONAL_KEYED_FORMS))
def test_a8_an_optional_keyed_dependency_is_injected(form):
    cls = _class_with(_OPTIONAL_KEYED_FORMS[form])
    container = Container()
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(cls)
    assert isinstance(container.resolve(cls).a, _DepA2), f"{form}: the keyed registration was ignored"


@pytest.mark.parametrize("form", list(_OPTIONAL_KEYED_FORMS))
def test_a8_an_optional_keyed_dependency_without_a_registration_keeps_its_default(form):
    cls = _class_with(_OPTIONAL_KEYED_FORMS[form])
    container = Container()
    container.register_transient(cls)
    assert container.resolve(cls).a is None


class _DepPipeA8:  # its own class: typing's cache must not hand back an equal Optional[...] form
    pass


def test_a8_a_pipe_union_inside_annotated_is_unwrapped():
    annotation = Annotated[_DepPipeA8 | None, Keyed("pipe")]
    inner = get_args(annotation)[0]
    if sys.version_info < (3, 14):                            # this test is about types.UnionType
        assert type(inner) is types.UnionType, f"not the types.UnionType form: {type(inner)!r}"
    cls = _class_with(annotation, name="PipeA8")
    container = Container()
    container.register_keyed(_DepPipeA8, "pipe", _DepPipeA8)
    container.register_transient(cls)
    assert isinstance(container.resolve(cls).a, _DepPipeA8), "the keyed registration was ignored"


def test_a8_validation_follows_an_optional_keyed_dependency(graph):
    from py_autowired import LifetimeScope
    container, patch = graph
    holder = _class_with(Optional[Annotated[_ScopedA7, Keyed("s")]], name="HolderA8")
    container.register_keyed(_ScopedA7, "s", _ScopedA7, scope=LifetimeScope.SCOPED)
    container.register_singleton(holder)
    patch(_consumer("holdera8_instance"))
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError), "the keyed scoped capture was not seen"
    assert any("captures scoped" in issue for issue in error.issues)


_UNSUPPORTED_KEYED_FORMS = {
    "union_other": Union[Annotated[_DepA2, Keyed("x")], int],
    "union_three": Union[None, Annotated[_DepA2, Keyed("x")], int],
    "in_a_list": list[Annotated[_DepA2, Keyed("x")]],
    "in_a_callable": Callable[[Annotated[_DepA2, Keyed("x")]], int],
}


@pytest.mark.parametrize("form", list(_UNSUPPORTED_KEYED_FORMS))
def test_a8_an_unsupported_keyed_form_is_never_silent(form, caplog):
    cls = _class_with(_UNSUPPORTED_KEYED_FORMS[form], name=f"Unsupported_{form}")
    container = Container()
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(cls)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        container.resolve(cls)
        container.resolve(cls)
    assert len(_warnings(caplog, f"Unsupported_{form}.a", "Keyed('x')", "not supported")) == 1


@pytest.mark.parametrize("form", list(_UNSUPPORTED_KEYED_FORMS))
def test_a8_validation_lists_an_unsupported_keyed_form(form, graph):
    container, patch = graph
    name = "Listed" + form.title().replace("_", "")
    cls = _class_with(_UNSUPPORTED_KEYED_FORMS[form], name=name)
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(cls)
    patch(_consumer(f"{name.lower()}_instance"))
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError), "an ignored Keyed passed validation"
    assert any(f"{name}.a" in issue and "Keyed('x')" in issue for issue in error.issues)


def test_a8_a_class_without_metadata_is_not_searched_for_keyed(monkeypatch):
    seen = []
    real = container_module._unsupported_keyed
    monkeypatch.setattr(container_module, "_unsupported_keyed", lambda *a: seen.append(a) or real(*a))
    container = Container()
    container.register_transient(_DepA2)
    container.register_transient(_PlainA2)
    container.resolve(_PlainA2)
    assert seen == []                                         # the hot path pays nothing


def test_a8_a_lifetime_only_follow_reports_lifetime_problems_only(graph):
    """Through Lazy only lifetime problems are followed (as for an unregistered dependency)."""
    container, patch = graph
    inner = _class_with(Union[Annotated[_DepA2, Keyed("x")], int], name="InnerA8")
    holder = type("LazyHolderA8", (), {"__module__": __name__})

    def __init__(self, inner: Lazy[inner]):
        self.inner = inner

    holder.__init__ = __init__
    container.register_keyed(_DepA2, "x", _DepA2)
    container.register_transient(inner)
    container.register_singleton(holder)
    patch(_consumer("lazyholdera8_instance"))
    issues = getattr(_call(validate_autowiring, container), "issues", ())
    assert [issue for issue in issues if "Keyed('x')" in issue] == []


# ---- A6: on 3.14 an unresolvable annotation does not break the signature itself -------------

_NEEDS_314 = pytest.mark.skipif(sys.version_info < (3, 14), reason="deferred annotations (PEP 649)")


def _module_with_forward_reference():
    module = types.ModuleType("a6_forward_reference")
    exec(textwrap.dedent("""
        class ForwardA6:
            def __init__(self, dep: UndefinedNameA6):
                self.dep = dep

        class ForwardDefaultA6:
            def __init__(self, dep: UndefinedNameA6 = None):
                self.dep = dep
    """), module.__dict__)
    return module


@_NEEDS_314
def test_a6_the_container_reports_an_unresolvable_annotation_in_its_own_way():
    module = _module_with_forward_reference()
    container = Container()
    container.register_transient(module.ForwardA6)
    container.register_transient(module.ForwardDefaultA6)
    error = _call(container.resolve, module.ForwardA6)
    assert isinstance(error, ServiceNotRegisteredError), f"raw error: {error!r}"
    assert container.resolve(module.ForwardDefaultA6).dep is None


@_NEEDS_314
def test_a6_validation_lists_an_unresolvable_annotation(graph):
    container, patch = graph
    module = _module_with_forward_reference()
    container.register_transient(module.ForwardA6)
    patch(_consumer("forwarda6_instance"))
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError), f"raw error: {error!r}"
    assert any("ForwardA6.dep: type hint cannot be resolved" in issue for issue in error.issues)


@_NEEDS_314
def test_a6_a_class_with_an_unresolvable_annotation_can_be_patched():
    module = _module_with_forward_reference()
    cls = type("PatchedA6", (module.ForwardA6,), {"clock_instance": None, "__module__": __name__})
    cls.__init__ = module.ForwardA6.__init__
    error = _call(autowired._patch_init, cls, Container(), {})
    assert error is None, f"raw error: {error!r}"


def test_a6_other_errors_of_inspect_signature_are_not_swallowed(monkeypatch):
    """Guard (all versions): only an annotation NameError on 3.14+ takes the new path."""
    def broken(*args, **kwargs):
        raise RuntimeError("inspect broke")

    monkeypatch.setattr(container_module, "inspect", types.SimpleNamespace(signature=broken))
    with pytest.raises(RuntimeError):
        container_module._init_signature(_PositionalA1)
