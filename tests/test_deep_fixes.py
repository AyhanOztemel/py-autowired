"""Turn 9 regressions: diagnostics preserve automatic field semantics."""

import asyncio
import inspect
import logging
from pathlib import Path
import sys
import threading
from types import ModuleType

import pytest

import py_autowired
from py_autowired import (
    AutoInjectionResolutionError, AutowiringValidationError, Container,
    DependencyInjectionError, LifetimeScope, ScopeNotActiveError,
    factory_dependencies, validate_autowiring,
)
from py_autowired import autowired


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


@pytest.mark.parametrize("context", ["feature.consumer", ""])
def test_02_tied_candidates_keep_first_silently_and_validate(graph, caplog, context):
    container, patch = graph
    first = type("Clock", (), {"__module__": "feature.first"})
    second = type("Clock", (), {"__module__": "feature.other"})
    consumer = type("Consumer", (), {"__module__": context, "clock_instance": None})
    container.register_transient(first)
    container.register_transient(second)
    patch(consumer)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        assert type(consumer().clock_instance) is first
        assert type(consumer().clock_instance) is first
    # No runtime log (logging during resolution could deadlock custom handlers);
    # the first registration still wins and opt-in validation reports the tie.
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    with pytest.raises(AutowiringValidationError, match="ambiguous candidates"):
        validate_autowiring(container)


@pytest.mark.parametrize("scope", list(LifetimeScope))
def test_03_sync_factory_rejects_and_closes_coroutine(scope):
    container = Container()
    coroutines, errors = [], []
    class Service:
        pass
    async def create():
        return Service()
    def factory(owner):
        result = create()
        coroutines.append(result)
        return result
    container.register_factory(Service, factory, scope)
    try:
        with container.create_scope():
            for _ in range(2):
                try:
                    container.resolve(Service)
                except DependencyInjectionError as exc:
                    errors.append(exc)
        assert len(errors) == 2, "awaitable must never be returned or cached as a service"
        assert all(type(error).__name__ == "AsyncFactoryError" for error in errors)
        assert getattr(py_autowired, "AsyncFactoryError", None) is type(errors[0])
        assert all(inspect.getcoroutinestate(c) == inspect.CORO_CLOSED for c in coroutines)
        assert not container.registrations[Service].has_instance
    finally:
        for coroutine in coroutines:
            coroutine.close()


def test_04_field_read_failure_warns_and_still_skips(graph, caplog):
    container, patch = graph
    class Consumer:
        # 0.5.4: only a settable property can be a field (a read-only one can
        # never receive a value); its failing getter still only warns.
        @property
        def repository_instance(self):
            raise RuntimeError("resource unavailable")

        @repository_instance.setter
        def repository_instance(self, value):
            pass
    patch(Consumer)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        consumer = Consumer()
    assert isinstance(consumer, Consumer)
    assert "Consumer.repository_instance" in caplog.text
    assert "resource unavailable" in caplog.text
    assert caplog.records[0].exc_info[0] is RuntimeError


@pytest.mark.parametrize("asynchronous", [False, True])
def test_05_multiple_disposal_errors_keep_first_notes_and_logs(caplog, asynchronous):
    container = Container()
    first, second = ValueError("first failure"), LookupError("second failure")
    calls = []
    class Earlier:
        def close(self):
            calls.append("earlier")
            raise second
    class Later:
        def close(self):
            calls.append("later")
            raise first
    container.register_scoped(Earlier)
    container.register_scoped(Later)
    scope = container.create_scope()
    with caplog.at_level(logging.ERROR, logger="py_autowired"):
        with pytest.raises(ValueError) as error:
            if asynchronous:
                async def run():
                    async with scope:
                        container.resolve(Earlier)
                        container.resolve(Later)
                asyncio.run(run())
            else:
                with scope:
                    container.resolve(Earlier)
                    container.resolve(Later)
    assert error.value is first and calls == ["later", "earlier"]
    assert any("LookupError" in note and "second failure" in note
               for note in getattr(first, "__notes__", ()))
    # The first error is raised (not logged); every additional one is logged.
    assert "second failure" in caplog.text
    assert scope.state.instances == {}


def test_11_untyped_required_constructor_is_validation_only(graph):
    container, patch = graph
    class Broken:
        def __init__(self, required):
            pytest.fail("validation must not construct")
    class Consumer:
        broken_instance = None
    container.register_transient(Broken)
    patch(Consumer)
    issues = []
    try:
        validate_autowiring(container)
    except AutowiringValidationError as exc:
        issues = exc.issues
    assert any("Broken.required needs a type annotation or default" in issue for issue in issues)
    with pytest.raises(AutoInjectionResolutionError) as error:
        Consumer()
    assert "needs a type annotation or default" in str(error.value.__cause__)


@pytest.mark.parametrize("route", ["constructor", "field", "factory", "indirect", "existing"])
def test_12_singleton_scoped_capture_is_validation_only(graph, route):
    container, patch = graph
    class Repository:
        pass
    class Middle:
        def __init__(self, repository: Repository):
            self.repository = repository
    class Owner:
        pass
    if route == "constructor":
        Owner = Middle
    elif route == "field":
        Owner.repository_instance = None
    elif route == "indirect":
        class Owner:
            def __init__(self, middle: Middle):
                self.middle = middle
    container.register_scoped(Repository)
    container.register_transient(Middle)
    if route == "factory":
        @factory_dependencies(Repository)
        def factory(owner):
            return owner.resolve(Repository)
        container.register_factory(Owner, factory, LifetimeScope.SINGLETON)
    else:
        container.register_singleton(Owner)
    consumer = type("Consumer", (), {"__module__": __name__,
                                    Owner.__name__.lower() + "_instance": None})
    patch(Owner, consumer)
    if route == "existing":
        # An already built singleton still has statically visible field edges.
        Owner.repository_instance = None
        # Recompute the test fixture's metadata before first construction.
        Owner.__di_attrs__ = ("repository_instance",)
        container.registrations[Owner].instance = Owner()
    issues = []
    try:
        validate_autowiring(container)
    except AutowiringValidationError as exc:
        issues = exc.issues
    assert any("singleton" in issue and "scoped" in issue and "Repository" in issue for issue in issues)
    with container.create_scope():
        assert container.resolve(Owner) is container.resolve(Owner)





def test_18_discovery_warns_on_wrong_module_origin(tmp_path, monkeypatch, caplog):
    module = ModuleType("origin_collision_probe")
    module.__file__ = str(tmp_path / "elsewhere" / "origin_collision_probe.py")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    expected = tmp_path / "origin_collision_probe.py"
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        result = autowired._import_module_safely(module.__name__, str(expected))
    assert result is False
    assert str(expected) in caplog.text and str(Path(module.__file__).resolve()) in caplog.text
    assert sys.modules[module.__name__] is module


def test_19_inherited_unannotated_field_is_reported_without_new_injection(tmp_path, monkeypatch, caplog):
    module = ModuleType("inherited_probe")
    module.__file__ = str(tmp_path / "inherited_probe.py")
    exec("class Repository: pass\nclass Base:\n    repository_instance = None\n"
         "class Child(Base):\n    def __init__(self): pass\n", module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    container = Container()
    container.register_transient(module.Repository)
    autowired.auto_inject(container, root_dir=str(tmp_path))
    assert module.Child().repository_instance is None
    # Informational WARNING only: validation must not fail on this pattern.
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        validate_autowiring(container)
    assert "Child.repository_instance" in caplog.text
    assert "inherited unannotated field is not injected" in caplog.text
    assert module.Child().repository_instance is None


def test_21_publish_requires_same_commit_complete_test_workflow():
    workflows = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    tests = (workflows / "tests.yml").read_text(encoding="utf-8")
    release = (workflows / "release.yml").read_text(encoding="utf-8")
    assert "  workflow_call:" in tests
    assert 'python-version: ["3.10", "3.14"]' in tests and 'python-version: "3.14t"' in tests
    assert "  compatibility:\n    uses: ./.github/workflows/tests.yml" in release
    assert "    needs: [build, compatibility]" in release
    assert "if: always()" not in release


def test_22_automatic_resolution_error_belongs_to_both_families():
    error = AutoInjectionResolutionError("failure")
    assert isinstance(error, RuntimeError)
    assert isinstance(error, DependencyInjectionError)


def test_05b_unprintable_additional_disposal_error_never_masks_first():
    container = Container()
    first = ValueError("first failure")

    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("format")

    class Earlier:
        def close(self):
            raise Unprintable()

    class Later:
        def close(self):
            raise first

    container.register_scoped(Earlier)
    container.register_scoped(Later)
    with pytest.raises(ValueError) as error:
        with container.create_scope():
            container.resolve(Earlier)
            container.resolve(Later)
    assert error.value is first
