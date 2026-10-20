"""Sixth wave: last testable survivors of round 5."""

import re
import sys
import threading
import types
from typing import Generic, TypeVar

import pytest

from py_autowired import (
    CircularDependencyError, Container, Keyed, LifetimeScope, autowired, factory_dependencies,
    validate_autowiring,
)
from py_autowired import container as core

T = TypeVar("T")


class Dep:
    pass


class VarArgsThenKeyword:
    def __init__(self, *args, dep: Dep, **kwargs):
        self.dep = dep


def test_var_args_do_not_stop_keyword_injection():
    container = Container()
    container.register_transient(Dep)
    container.register_transient(VarArgsThenKeyword)
    assert isinstance(container.resolve(VarArgsThenKeyword).dep, Dep)


def test_keyed_inequality_both_directions():
    assert Keyed("a") != Keyed("b") and Keyed("b") != Keyed("a")


def test_same_module_wins_regardless_of_name_order(monkeypatch):
    same = type("Clock", (), {"__module__": "app.b"})
    near = type("Clock", (), {"__module__": "app.b.x"})
    lower = type("Clock", (), {"__module__": "app.a"})
    for regs_order in ([lower, near], [near, lower]):
        container = Container()
        for cls in regs_order:
            container.register_transient(cls)
        cache = autowired._CachedNameMap(container)
        chosen = cache.get_cached_registration("clock", "app.b")
        assert chosen.service_type is near          # "app.a" must not win by sorting lower
    container = Container()
    container.register_transient(near)
    container.register_transient(same)
    cache = autowired._CachedNameMap(container)
    assert cache.get_cached_registration("clock", "app.b").service_type is same


def test_cross_thread_cycle_message_names_the_closed_loop():
    class A:
        pass

    class B:
        pass

    barrier = threading.Barrier(2, timeout=5)
    first = {"A": True, "B": True}

    def once(name):
        if first[name]:
            first[name] = False
            barrier.wait()

    def make_a(c):
        once("A")
        c.resolve(B)
        return A()

    def make_b(c):
        once("B")
        c.resolve(A)
        return B()

    container = Container()
    container.register_factory(A, make_a, LifetimeScope.SINGLETON)
    container.register_factory(B, make_b, LifetimeScope.SINGLETON)
    errors = []

    def run(service):
        try:
            container.resolve(service)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s,), daemon=True) for s in (A, B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    messages = [str(e) for e in errors if isinstance(e, CircularDependencyError) and "across threads" in str(e)]
    assert messages, errors
    assert all(re.search(r"across threads: (A -> B -> A|B -> A -> B)$", m) for m in messages), messages


def test_validation_never_populates_closed_generic_cache_and_accepts_registered_factory_deps(monkeypatch):
    class Box(Generic[T]):
        pass

    class Uses:
        def __init__(self, box: Box[int]):
            pass

    @factory_dependencies(Dep)
    def build(c):
        return object()

    class IFoo:
        pass

    container = Container()
    container.register_open_generic(Box)
    container.register_transient(Uses)
    container.register_transient(Dep)
    container.register_factory(IFoo, build)
    consumer = type("Consumer", (), {"__module__": "app.c", "uses_instance": None, "foo_instance": None})
    name_map = autowired._build_name_map_from_container(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._CachedNameMap(container))
    autowired._patch_setattr(consumer, container, name_map)
    autowired._patch_init(consumer, container, name_map)
    validate_autowiring(container)                     # no issue at all
    assert container._closed_generics == {}


def test_uninspectable_constructor_counts_as_accepting_no_arguments(monkeypatch):
    class Odd:
        def __init__(self, required):
            pass

    core._accepts_no_arguments.cache_clear()

    def broken(*_args, **_kwargs):
        raise ValueError("no signature")

    monkeypatch.setattr(core.inspect, "signature", broken)
    assert core._accepts_no_arguments(Odd) is True
    core._accepts_no_arguments.cache_clear()

    def broken_type(*_args, **_kwargs):
        raise TypeError("no signature")

    monkeypatch.setattr(core.inspect, "signature", broken_type)
    assert core._accepts_no_arguments(Odd) is True
    core._accepts_no_arguments.cache_clear()


class _NoSetattrMeta(type):
    def __setattr__(cls, name, value):
        if name == "__di_attrs__":
            raise TypeError("read-only class")
        super().__setattr__(name, value)


def test_auto_inject_survives_odd_modules_and_classes(tmp_path, monkeypatch):
    name = "odd_mod_probe"
    good = types.ModuleType(name)
    good.__file__ = str(tmp_path / f"{name}.py")
    exotic = _NoSetattrMeta("Exotic", (), {})
    exotic.__module__ = name
    good.Exotic = exotic
    consumer = type("Consumer", (), {"__module__": name, "dep_instance": None})
    good.Consumer = consumer
    bad = types.ModuleType("zz_bad_file_probe")
    bad.__file__ = 12345                                   # Path(12345) raises TypeError
    monkeypatch.setitem(sys.modules, "aa_bad_file_probe", bad)
    monkeypatch.setitem(sys.modules, name, good)
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    monkeypatch.setattr(autowired, "_remember_patched_class",
                        lambda *_: (_ for _ in ()).throw(RuntimeError("bookkeeping")))
    container = Container()
    container.register_transient(Dep)
    autowired.auto_inject(container, root_dir=str(tmp_path))
    assert isinstance(consumer().dep_instance, Dep)


def test_subclass_walk_continues_past_an_already_seen_child():
    import abc

    class Base(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    class Mid(Base):                      # abstract
        pass

    class Diamond(Mid, Base):             # direct child of BOTH Mid and Base
        def run(self): ...

    class Later(Base):                    # listed after Diamond under Base
        def run(self): ...

    found = core._concrete_subclasses(Base)
    assert Diamond in found and Later in found and len(found) == 2
