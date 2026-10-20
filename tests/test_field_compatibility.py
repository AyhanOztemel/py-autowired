"""Measured field behavior; these tests intentionally do not add special cases."""

from dataclasses import dataclass

import pytest

from py_autowired import AutoInjectionResolutionError, Container, autowired


class Repository:
    pass


@pytest.fixture
def patch(monkeypatch):
    container = Container()
    container.register_singleton(Repository)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)

    def install(cls):
        name_map = cache.get_raw_map()
        autowired._patch_setattr(cls, container, name_map)
        autowired._patch_init(cls, container, name_map)
        return cls

    return install


@pytest.mark.parametrize("slots", [False, True])
def test_mutable_dataclass_supports_typed_fields_and_preserves_override(patch, slots):
    @dataclass(slots=slots)
    class Consumer:
        repository_instance: Repository | None = None

    patch(Consumer)
    first, second = Consumer(), Consumer()
    assert isinstance(first.repository_instance, Repository)
    assert first.repository_instance is second.repository_instance
    manual = Repository()
    assert Consumer(manual).repository_instance is manual


def test_slots_support_requires_an_initialized_slot(patch):
    class Consumer:
        __slots__ = ("repository_instance",)
        repository_instance: Repository | None

        def __init__(self):
            self.repository_instance = None

    patch(Consumer)
    assert isinstance(Consumer().repository_instance, Repository)

    class Uninitialized:
        __slots__ = ("repository_instance",)
        repository_instance: Repository | None

    patch(Uninitialized)
    assert not hasattr(Uninitialized(), "repository_instance")


@pytest.mark.parametrize("slots", [False, True])
def test_frozen_dataclass_rejects_post_init_injection(patch, slots):
    @dataclass(frozen=True, slots=slots)
    class Consumer:
        repository_instance: Repository | None = None

    patch(Consumer)
    with pytest.raises(AutoInjectionResolutionError) as error:
        Consumer()
    assert isinstance(error.value.__cause__, (AttributeError, TypeError))


def test_pydantic_basemodel_requires_arbitrary_types_and_injects_after_validation(patch):
    pydantic = pytest.importorskip("pydantic")
    seen = []

    class Consumer(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(arbitrary_types_allowed=True)
        repository_instance: Repository | None = None

        def model_post_init(self, context):
            seen.append(self.repository_instance)

    patch(Consumer)
    assert isinstance(Consumer().repository_instance, Repository)
    assert seen == [None]  # Pydantic's hook runs before our post-init pass.
    manual = Repository()
    assert Consumer(repository_instance=manual).repository_instance is manual


def test_frozen_pydantic_basemodel_rejects_injection(patch):
    pydantic = pytest.importorskip("pydantic")

    class Consumer(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(arbitrary_types_allowed=True, frozen=True)
        repository_instance: Repository | None = None

    patch(Consumer)
    with pytest.raises(AutoInjectionResolutionError) as error:
        Consumer()
    assert isinstance(error.value.__cause__, pydantic.ValidationError)


@pytest.mark.parametrize("slots", [False, True])
def test_mutable_attrs_supports_typed_fields(patch, slots):
    attrs = pytest.importorskip("attrs")

    @attrs.define(slots=slots)
    class Consumer:
        repository_instance: Repository | None = None

    patch(Consumer)
    assert isinstance(Consumer().repository_instance, Repository)
    manual = Repository()
    assert Consumer(manual).repository_instance is manual


def test_frozen_attrs_rejects_injection(patch):
    attrs = pytest.importorskip("attrs")

    @attrs.frozen
    class Consumer:
        repository_instance: Repository | None = None

    patch(Consumer)
    with pytest.raises(AutoInjectionResolutionError) as error:
        Consumer()
    assert isinstance(error.value.__cause__, attrs.exceptions.FrozenInstanceError)


def test_annotation_is_documentation_and_none_default_is_required(patch):
    class Consumer:
        # The name still selects Repository even though the annotation differs.
        repository_instance: str | None = None

    class AnnotationOnly:
        repository_instance: Repository | None

    patch(Consumer)
    patch(AnnotationOnly)
    assert isinstance(Consumer().repository_instance, Repository)
    assert not hasattr(AnnotationOnly(), "repository_instance")
