"""Python 3.10 adds an implicit Optional for `x: T = None` in get_type_hints;
3.11+ does not. Injection must not depend on the interpreter version."""

from typing import Optional

from py_autowired import Container


class Dep:
    pass


class ImplicitNone:
    def __init__(self, dep: Dep = None):
        self.dep = dep


class ExplicitOptional:
    def __init__(self, dep: Optional[Dep] = None):
        self.dep = dep


def test_none_default_is_injected_on_every_version():
    container = Container()
    container.register_transient(Dep)
    container.register_transient(ImplicitNone)
    assert isinstance(container.resolve(ImplicitNone).dep, Dep)


def test_explicit_optional_keeps_its_previous_behavior():
    container = Container()
    container.register_transient(Dep)
    container.register_transient(ExplicitOptional)
    assert container.resolve(ExplicitOptional).dep is None


class NoneHandler:
    pass


class UsesNoneNamedType:
    def __init__(self, handler: "NoneHandler" = None):
        self.handler = handler


def test_type_whose_name_contains_none_is_still_injected():
    container = Container()
    container.register_transient(NoneHandler)
    container.register_transient(UsesNoneNamedType)
    assert isinstance(container.resolve(UsesNoneNamedType).handler, NoneHandler)
