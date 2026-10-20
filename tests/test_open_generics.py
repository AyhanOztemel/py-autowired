"""Opt-in open generic registration (.NET open generics):
register_open_generic(Repository, SqlRepository) serves Repository[User],
Repository[Order], ... Each closed type has its own lifetime/scope slot."""

import types
from typing import Generic, TypeVar

import pytest

from py_autowired import (
    Container, LifetimeScope, ServiceNotRegisteredError, autowired, validate_autowiring,
)

T = TypeVar("T")


class User:
    pass


class Order:
    pass


class Repository(Generic[T]):
    pass


class SqlRepository(Repository[T]):
    def __init__(self, entity: type[T] = None):
        self.entity = entity


class Mapper(Generic[T]):
    def __init__(self, item: T):
        self.item = item


def test_closed_types_resolve_from_one_open_registration():
    container = Container()
    container.register_open_generic(Repository, SqlRepository)
    users, orders = container.resolve(Repository[User]), container.resolve(Repository[Order])
    assert isinstance(users, SqlRepository) and isinstance(orders, SqlRepository)
    assert users is not orders
    with pytest.raises(ServiceNotRegisteredError):
        container.resolve(Mapper[User])


def test_lifetime_is_per_closed_type():
    container = Container()
    container.register_open_generic(Repository, SqlRepository, LifetimeScope.SINGLETON)
    assert container.resolve(Repository[User]) is container.resolve(Repository[User])
    assert container.resolve(Repository[User]) is not container.resolve(Repository[Order])
    container2 = Container()
    container2.register_open_generic(Repository, SqlRepository, LifetimeScope.SCOPED)
    with container2.create_scope():
        a = container2.resolve(Repository[User])
        assert container2.resolve(Repository[User]) is a
    with container2.create_scope():
        assert container2.resolve(Repository[User]) is not a


def test_typevar_constructor_parameter_is_substituted():
    container = Container()
    container.register_transient(User)
    container.register_open_generic(Mapper, Mapper)
    mapped = container.resolve(Mapper[User])
    assert isinstance(mapped.item, User)


def test_constructor_parameter_with_closed_generic_is_injected():
    class UserService:
        def __init__(self, users: Repository[User]):
            self.users = users

    container = Container()
    container.register_open_generic(Repository, SqlRepository)
    container.register_transient(UserService)
    assert isinstance(container.resolve(UserService).users, SqlRepository)
    assert container.is_registered(Repository[User])
    assert not container.is_registered(Mapper[User])


def test_open_generics_do_not_change_field_matching_or_validation(monkeypatch):
    module = types.ModuleType("generic_probe")
    exec(
        "from py_autowired import Container\n"
        "class UserService:\n"
        "    def __init__(self, users: 'Repo'):\n"
        "        self.users = users\n"
        "class Consumer:\n"
        "    user_service_instance = None\n"
        "    repository_instance = None\n",
        module.__dict__,
    )
    module.Repo = Repository[User]
    module.UserService.__init__.__annotations__["users"] = Repository[User]
    monkeypatch.setitem(__import__("sys").modules, module.__name__, module)
    container = Container()
    container.register_open_generic(Repository, SqlRepository)
    container.register_transient(module.UserService)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(module.Consumer, container, cache.get_raw_map())
    autowired._patch_init(module.Consumer, container, cache.get_raw_map())
    consumer = module.Consumer()
    assert isinstance(consumer.user_service_instance.users, SqlRepository)
    assert consumer.repository_instance is None      # open generics are not name-matched
    with pytest.raises(Exception) as exc:              # only the unmatched field is reported
        validate_autowiring(container)
    assert "repository_instance: no matching registration" in str(exc.value)
    assert "Repository" not in str(exc.value).split("repository_instance")[0]


class FastRepository(Repository[T]):
    pass


def test_reregistering_an_open_generic_replaces_cached_closed_types():
    container = Container()
    container.register_open_generic(Repository, SqlRepository)
    assert isinstance(container.resolve(Repository[User]), SqlRepository)
    container.register_open_generic(Repository, FastRepository)
    assert isinstance(container.resolve(Repository[User]), FastRepository)
