"""Opt-in diagnostics must never become part of the runtime resolution path."""

from abc import ABC, abstractmethod
import gc
import inspect
import sys
from types import ModuleType
import weakref

import pytest

from py_autowired import (
    AutowiringValidationError, Container, factory_dependencies, validate_autowiring,
)
from py_autowired import autowired


@pytest.fixture
def graph(monkeypatch):
    container = Container()
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._CachedNameMap(container))

    def patch(*classes):
        name_map = autowired._build_name_map_from_container(container)
        for cls in classes:
            autowired._patch_setattr(cls, container, name_map)
            autowired._patch_init(cls, container, name_map)
        return container

    return container, patch


def test_collects_missing_ambiguous_and_unresolvable_without_construction(graph, monkeypatch):
    container, patch = graph
    calls = []
    first = type("Shared", (), {"__module__": "app.first"})
    second = type("Shared", (), {"__module__": "app.other"})

    class Missing:
        pass

    class Broken:
        def __init__(self, required):
            calls.append("constructor")

    Broken.__init__.__annotations__["required"] = Missing

    class Consumer:
        missing_instance = None
        shared_instance = None
        broken_instance = None

    container.register_transient(first)
    container.register_transient(second)
    container.register_transient(Broken)
    patch(Consumer)
    cache = autowired._NAME_MAP_CACHE
    snapshot = dict(cache._resolution_cache)
    history = list(container.registration_history)
    monkeypatch.setattr(container, "resolve", lambda *_: pytest.fail("resolve must not run"))
    with pytest.raises(AutowiringValidationError) as error:
        validate_autowiring(container)
    assert len(error.value.issues) == 3
    assert "missing_instance: no matching registration" in str(error.value)
    assert "shared_instance: ambiguous candidates:" in str(error.value)
    assert "broken_instance: registered but unresolvable:" in str(error.value)
    assert calls == []
    assert cache._resolution_cache == snapshot
    assert cache._raw_map is None
    assert container.registration_history == history
    assert container.current_scope is None
    assert all(not reg.has_instance for reg in container.registrations.values())


def test_clean_validation_deduplicates_aliases_and_does_not_call_factories(graph):
    container, patch = graph
    calls = []

    class Service:
        pass

    class ServiceAlias:
        pass

    class Consumer:
        service_instance: Service | None = None
        service_alias_instance: ServiceAlias | None = None

    container.register_factory(Service, lambda owner: calls.append(owner))
    container.register_alias(ServiceAlias, Service)
    patch(Consumer)
    assert validate_autowiring(container) is None
    assert calls == []


def test_module_proximity_is_resolved_and_ties_preserve_first_registration(graph):
    container, patch = graph
    far = type("Clock", (), {"__module__": "elsewhere.clock"})
    near = type("Clock", (), {"__module__": "feature.consumer"})
    consumer = type("Consumer", (), {"__module__": "feature.consumer", "clock_instance": None})
    container.register_transient(far)
    container.register_transient(near)
    patch(consumer)
    validate_autowiring(container)
    assert type(consumer().clock_instance) is near

    other = type("Clock", (), {"__module__": "feature.consumer"})
    container.register_transient(other)
    autowired.refresh()
    with pytest.raises(AutowiringValidationError, match="ambiguous candidates"):
        validate_autowiring(container)
    assert type(consumer().clock_instance) is near


@pytest.mark.parametrize("context,modules,expected", [
    ("", ("app.deep", "app"), 0),
    ("app.consumer", ("app.consumer", "app.near.deep", "elsewhere.clock"), 0),
    ("app.consumer", ("app.clock", "app.deep.clock"), 1),
    ("app.consumer", ("", "elsewhere"), 1),
    ("app.consumer", ("", ""), 0),
    ("app.consumer", ("app.near", "app.far"), 0),
    ("app.consumer", ("x.deep", "y.deep"), 0),
    ("app.consumer", ("app.consumer", ".".join(["x"] * 1001)), 1),
])
def test_validation_and_runtime_preserve_original_candidate_scores(graph, context, modules, expected):
    container, patch = graph
    implementations = [type("Clock", (), {"__module__": module}) for module in modules]
    for index, implementation in enumerate(implementations):
        # Binding modules deliberately disagree with implementation proximity.
        binding = type(f"Binding{index}", (), {"__module__": context})
        container.register_transient(binding, implementation)
    consumer = type("Consumer", (), {"__module__": context, "clock_instance": None})
    patch(consumer)
    regs = list(container.registrations.values())

    # Independent copy of the original rule guards the runtime refactor.
    def original_score(module):
        if not context:
            return (0, 0)
        if not module:
            return (-1, 0)
        common = 0
        for left, right in zip(context.split("."), module.split(".")):
            if left != right:
                break
            common += 1
        depth = len(module.split("."))
        return (1000 if module == context else common * 10 + depth, depth)

    scores = [original_score(module) for module in modules]
    assert [autowired._registration_score(reg, context) for reg in regs] == scores
    assert autowired._pick_best_registration(regs, context) is regs[expected]
    best = max(scores)
    tied = [index for index, score in enumerate(scores) if score == best]
    if len(tied) > 1:
        with pytest.raises(AutowiringValidationError) as error:
            validate_autowiring(container)
        assert len(error.value.issues) == 1
        assert error.value.issues[0].endswith("ambiguous candidates: " + ", ".join(
            f"{modules[index]}.Clock" for index in tied
        ))
    else:
        assert validate_autowiring(container) is None
    assert type(consumer().clock_instance) is implementations[expected]


def test_constructor_defaults_and_field_shortcut_follow_real_resolution(graph):
    container, patch = graph
    calls = []
    marker = object()

    class Missing:
        pass

    class Repository:
        pass

    class Defaulted:
        def __init__(self, dependency: Missing = marker, untyped=marker):
            calls.append("defaulted")
            self.dependency, self.untyped = dependency, untyped

    class Broken:
        def __init__(self, dependency: Missing):
            pytest.fail("field-only constructor dependency must be skipped")

    class Fields:
        repository_instance = None

        def __init__(self, dependency: Broken = marker):
            calls.append("fields")
            self.dependency = dependency

    class Mixed:
        repository_instance = None

        def __init__(self, dependency: Repository, optional: Missing = marker):
            calls.append("mixed")
            self.dependency, self.optional = dependency, optional

    class Consumer:
        defaulted_instance = None
        fields_instance = None
        mixed_instance = None

    container.register_singleton(Repository)
    for cls in (Defaulted, Broken, Fields, Mixed):
        container.register_transient(cls)
    patch(Fields, Mixed, Consumer)
    assert validate_autowiring(container) is None
    assert calls == []
    result = Consumer()
    assert calls == ["defaulted", "fields", "mixed"]
    assert result.defaulted_instance.dependency is result.defaulted_instance.untyped is marker
    assert result.fields_instance.dependency is marker
    assert isinstance(result.fields_instance.repository_instance, Repository)
    assert result.mixed_instance.dependency is result.mixed_instance.repository_instance
    assert result.mixed_instance.optional is marker


def test_factory_hides_constructor_and_abstract_details_from_validation(graph):
    container, patch = graph
    calls = []
    marker = object()

    class FactoryService(ABC):
        def __init__(self, required):
            pytest.fail("factory-backed constructor must not run")

        @abstractmethod
        def run(self):
            pass

    def factory(owner):
        calls.append(owner)
        return marker

    class Consumer:
        factory_service_instance = None

    container.register_factory(FactoryService, factory)
    patch(Consumer)
    assert validate_autowiring(container) is None
    assert calls == []
    assert Consumer().factory_service_instance is marker
    assert calls == [container]


@pytest.mark.parametrize("kind", ["forward_reference", "opaque", "positional_only"])
def test_uncertain_constructor_details_are_not_validation_errors(graph, kind):
    container, patch = graph

    class Service:
        pass

    class Unknown:
        def __init__(self, dependency):
            pytest.fail("validation must not invoke constructors")

    if kind == "forward_reference":
        Unknown.__init__.__annotations__["dependency"] = "UnavailableType"
    elif kind == "opaque":
        Unknown.__init__.__signature__ = object()
    elif kind == "positional_only":
        def constructor(self, dependency: Service, /):
            pytest.fail("validation must not invoke constructors")
        Unknown.__init__ = constructor

    class Consumer:
        unknown_instance = None

    container.register_transient(Service)
    container.register_transient(Unknown)
    patch(Consumer)
    assert validate_autowiring(container) is None


def test_registered_default_dependency_is_still_checked_like_runtime(graph):
    container, patch = graph

    class Missing:
        pass

    class Broken:
        def __init__(self, missing: Missing):
            pytest.fail("validation must not invoke constructors")

    class Defaulted:
        def __init__(self, dependency: Broken = None):
            self.dependency = dependency

    class Consumer:
        defaulted_instance = None

    container.register_transient(Broken)
    container.register_transient(Defaulted)
    patch(Consumer)
    with pytest.raises(AutowiringValidationError, match="unregistered dependency"):
        validate_autowiring(container)
    from py_autowired import ServiceNotRegisteredError
    with pytest.raises(ServiceNotRegisteredError):
        container.resolve(Defaulted)


def test_static_constructor_graph_collects_missing_and_cycle(graph):
    container, patch = graph

    class Missing:
        pass

    class Broken:
        def __init__(self, dependency):
            pytest.fail("constructor must not run")

    Broken.__init__.__annotations__["dependency"] = Missing
    container.register_transient(Broken)

    class Cycle:
        cycle_instance = None

    class Consumer:
        broken_instance = None
        cycle_instance = None

    container.register_transient(Cycle)
    patch(Cycle, Consumer)
    with pytest.raises(AutowiringValidationError) as error:
        validate_autowiring(container)
    assert "registered dependency" in str(error.value)
    assert "circular dependency: Cycle -> Cycle" in str(error.value)


def test_factory_metadata_and_abstract_implementation_are_checked(graph):
    container, patch = graph

    class Missing:
        pass

    class FactoryService:
        pass

    @factory_dependencies(Missing)
    def factory(owner):
        pytest.fail("factory must not run")

    class Abstract(ABC):
        @abstractmethod
        def run(self):
            pass

    class Consumer:
        factory_service_instance = None
        abstract_instance = None

    container.register_factory(FactoryService, factory)
    container.register_transient(Abstract, Abstract)
    patch(Consumer)
    with pytest.raises(AutowiringValidationError) as error:
        validate_autowiring(container)
    assert "factory requires unregistered" in str(error.value)
    assert "Abstract is abstract" in str(error.value)


def test_manual_class_value_and_property_are_not_executed(graph):
    container, patch = graph

    class Consumer:
        manual_instance = object()

        @property
        def unknown_instance(self):
            pytest.fail("descriptor must not run")

    patch(Consumer)
    with pytest.raises(AutowiringValidationError) as error:
        validate_autowiring(container)
    assert len(error.value.issues) == 1
    assert "unknown_instance" in str(error.value)


def test_validation_uses_supplied_container_even_when_another_is_active(graph):
    container, patch = graph

    class Consumer:
        missing_instance = None

    patch(Consumer)
    autowired.activate_container(Container())
    active = autowired._NAME_MAP_CACHE
    with pytest.raises(AutowiringValidationError, match="no matching registration"):
        validate_autowiring(container)
    assert autowired._NAME_MAP_CACHE is active


def test_auto_inject_validation_is_off_by_default_and_opt_in(tmp_path, monkeypatch):
    module = ModuleType("startup_probe")
    module.__file__ = str(tmp_path / "startup_probe.py")
    exec("class Consumer:\n    missing_instance = None\n", module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    container = Container()
    assert inspect.signature(autowired.auto_inject).parameters["validate"].default is False
    assert autowired.auto_inject(container, root_dir=str(tmp_path)) == 1
    assert module.Consumer().missing_instance is None
    with pytest.raises(AutowiringValidationError, match="no matching registration"):
        autowired.auto_inject(container, root_dir=str(tmp_path), validate=True)
    assert module.Consumer().missing_instance is None


def test_typed_startup_example_validates_without_opening_scope_or_creating_services(tmp_path, monkeypatch):
    module_name = "validated_product_graph"
    source = '''
created = []
class ProductRepository:
    def __init__(self):
        created.append("repository")
    def list_products(self):
        return ["Notebook", "Pencil"]
class ProductService:
    product_repository_instance: ProductRepository | None = None
    def list_products(self):
        return self.product_repository_instance.list_products()
class ProductController:
    product_service_instance: ProductService | None = None
    def list_products(self):
        return self.product_service_instance.list_products()
'''
    (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = __import__(module_name)
    monkeypatch.delitem(sys.modules, module_name)
    monkeypatch.setitem(sys.modules, module_name, module)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    container = Container()
    container.register_scoped(module.ProductRepository)
    container.register_transient(module.ProductService)
    autowired.auto_inject(container, root_dir=str(tmp_path), validate=True)
    assert module.created == []
    assert container.current_scope is None
    with container.create_scope():
        first = module.ProductController()
        second = module.ProductController()
        assert first.list_products() == ["Notebook", "Pencil"]
        assert first.product_service_instance is not second.product_service_instance
        assert first.product_service_instance.product_repository_instance is second.product_service_instance.product_repository_instance
    assert module.created == ["repository"]


def test_fastapi_validates_at_startup_only_when_requested(tmp_path, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from py_autowired.fastapi import attach_container

    module = ModuleType("startup_bridge_probe")
    module.__file__ = str(tmp_path / "startup_bridge_probe.py")
    exec("class Consumer:\n    missing_instance = None\n", module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    assert inspect.signature(attach_container).parameters["validate"].default is False
    for enabled in (False, True):
        app = fastapi.FastAPI()
        attach_container(app, Container(), root_dir=str(tmp_path), validate=enabled)
        if enabled:
            with pytest.raises(AutowiringValidationError):
                with TestClient(app):
                    pytest.fail("startup must fail")
        else:
            with TestClient(app):
                assert module.Consumer().missing_instance is None


def test_runtime_order_manual_none_and_selected_types_unchanged_by_validation(graph):
    container, patch = graph
    order = []
    marker = object()

    class First:
        pass

    class Second:
        pass

    def first(owner):
        order.append("first")
        return First()

    def second(owner):
        order.append("second")
        return Second()

    class Eager:
        def __init__(self):
            self.second_instance = None
            self.first_instance = None
            self.absent_instance = None
            self.manual_instance = marker

    class Fields:
        second_instance: Second | None = None
        first_instance: First | None = None
        absent_instance = None
        manual_instance = marker

    container.register_factory(First, first)
    container.register_factory(Second, second)
    patch(Eager, Fields)

    def snapshot():
        order.clear()
        eager = Eager()
        assert order == ["second", "first"]
        fields = Fields()
        assert order == ["second", "first", "first", "second"]
        assert eager.manual_instance is fields.manual_instance is marker
        assert eager.absent_instance is fields.absent_instance is None
        return type(eager.first_instance), type(fields.second_instance), tuple(order)

    before = snapshot()
    with pytest.raises(AutowiringValidationError):
        validate_autowiring(container)
    assert snapshot() == before == (First, Second, ("second", "first", "first", "second"))


def test_replace_recipe_keeps_automatic_field_injection(graph):
    container, patch = graph

    class ProductRepository:
        def list_products(self):
            return ["real"]

    class FakeProductRepository(ProductRepository):
        def list_products(self):
            return ["test"]

    class ProductService:
        product_repository_instance: ProductRepository | None = None

        def list_products(self):
            return self.product_repository_instance.list_products()

    class ProductController:
        product_service_instance: ProductService | None = None

        def list_products(self):
            return self.product_service_instance.list_products()

    container.register_transient(ProductRepository)
    container.register_transient(ProductService)
    patch(ProductService, ProductController)
    assert ProductController().list_products() == ["real"]
    container.replace(ProductRepository, FakeProductRepository)
    autowired.refresh()
    validate_autowiring(container)
    assert ProductController().list_products() == ["test"]


def test_unhashable_container_subclasses_keep_existing_autowiring(monkeypatch):
    class UnhashableContainer(Container):
        __hash__ = None

    container = UnhashableContainer()

    class Service:
        pass

    class Consumer:
        service_instance: Service | None = None

    container.register_transient(Service)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(Consumer, container, cache.get_raw_map())
    autowired._patch_init(Consumer, container, cache.get_raw_map())
    validate_autowiring(container)
    assert isinstance(Consumer().service_instance, Service)


@pytest.mark.parametrize("validate", [False, True])
@pytest.mark.parametrize("eager", [False, True])
def test_unhashable_classes_keep_startup_and_automatic_field_injection(
    tmp_path, monkeypatch, validate, eager,
):
    module = ModuleType("unhashable_class_probe")
    module.__file__ = str(tmp_path / "unhashable_class_probe.py")
    source = '''
created = []
class UnhashableMeta(type):
    __hash__ = None
class Service:
    def __init__(self):
        created.append("service")
class Consumer(metaclass=UnhashableMeta):
    service_instance = None
'''
    if eager:
        source += "    def __init__(self, value=None):\n        self.service_instance = value\n"
    exec(source, module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    container = Container()
    container.register_singleton(module.Service)
    with pytest.raises(TypeError):
        hash(module.Consumer)

    options = {"validate": True} if validate else {}
    assert autowired.auto_inject(container, root_dir=str(tmp_path), **options) == 1
    assert module.created == []
    consumer = module.Consumer()
    assert isinstance(consumer.service_instance, module.Service)
    assert module.created == ["service"]
    marker = object()
    consumer.service_instance = marker
    assert consumer.service_instance is marker
    if eager:
        assert module.Consumer(marker).service_instance is marker


def test_unhashable_classes_are_included_in_validation_report(graph):
    container, patch = graph

    class UnhashableMeta(type):
        __hash__ = None

    class Consumer(metaclass=UnhashableMeta):
        missing_instance = None

    patch(Consumer)
    assert Consumer().missing_instance is None
    with pytest.raises(AutowiringValidationError, match="missing_instance: no matching registration"):
        validate_autowiring(container)


def test_patched_class_tracking_deduplicates_without_retaining_classes(graph):
    container, patch = graph

    class UnhashableMeta(type):
        __hash__ = None

    def track():
        class Consumer(metaclass=UnhashableMeta):
            missing_instance = None

        patch(Consumer)
        patch(Consumer)
        assert len(autowired._PATCHED_CLASSES[id(container)]) == 1
        return weakref.ref(Consumer)

    reference = track()
    gc.collect()
    assert reference() is None
    assert len(autowired._PATCHED_CLASSES[id(container)]) == 0
    assert validate_autowiring(container) is None
