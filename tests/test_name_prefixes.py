"""0.5.1: every common interface / abstract-class prefix matches the field name.

``product_repository_instance`` must find the registration whatever prefix the
contract uses: I, I_, ABS, ABS_, Abs, Abs_, abs, abs_ (or the Impl suffix).
"""

import abc

import pytest

from py_autowired import Container, autowired

PREFIXED = [
    "IProductRepository", "I_ProductRepository",
    "ABSProductRepository", "ABS_ProductRepository",
    "AbsProductRepository", "Abs_ProductRepository",
    "absProductRepository", "abs_ProductRepository",
    "ProductRepositoryImpl",
]


def _contract(name):
    return type(name, (abc.ABC,), {"list_products": abc.abstractmethod(lambda self: None)})


@pytest.mark.parametrize("name", PREFIXED)
def test_prefix_matches_the_field_name(name):
    contract = _contract(name)
    implementation = type("Memory" + name, (contract,), {"list_products": lambda self: ["ok"]})
    container = Container()
    container.register_singleton(contract, implementation)
    name_map = autowired._build_name_map_from_container(container)
    assert "productrepository" in name_map


@pytest.mark.parametrize("name", ["Absolute", "Abstract", "Ignored", "ABSTRACT"])
def test_words_that_merely_start_like_a_prefix_keep_their_own_key(name):
    contract = _contract(name + "Thing")
    container = Container()
    container.register_singleton(contract, type("Impl" + name, (contract,), {"list_products": lambda s: 1}))
    name_map = autowired._build_name_map_from_container(container)
    assert autowired._norm(name + "Thing") in name_map


def test_i_underscore_contract_is_injected_end_to_end(monkeypatch):
    contract = _contract("I_ProductRepository")
    implementation = type("MemoryProductRepository", (contract,), {"list_products": lambda self: ["ok"]})
    consumer = type("Consumer", (), {"__module__": "app.consumer", "product_repository_instance": None})
    container = Container()
    container.register_singleton(contract, implementation)
    name_map = autowired._build_name_map_from_container(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._CachedNameMap(container))
    autowired._patch_setattr(consumer, container, name_map)
    autowired._patch_init(consumer, container, name_map)
    assert consumer().product_repository_instance.list_products() == ["ok"]
