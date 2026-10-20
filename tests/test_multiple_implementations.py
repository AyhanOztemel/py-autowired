"""0.5.2: several implementations of one contract, as in .NET DI and Spring.

``register_scoped(IRepo, MemoryRepo)`` followed by ``register_scoped(IRepo, SqlRepo)``
keeps both. A field named after a class receives that class, the contract's own
name receives the last registration (unchanged), and a plural field
(``repo_instances``) receives all of them. Field names may use upper case.
"""

import abc
import importlib
import sys

import pytest

from py_autowired import (
    AutoInjectionResolutionError, AutowiringValidationError, Container,
    DuplicateServiceRegistrationError, ServiceNotRegisteredError, autowired,
    validate_autowiring,
)
from py_autowired import container as core


def _family(contract_name="IRepo", *names):
    """A fresh contract and concrete classes; every test gets its own types."""
    contract = type(contract_name, (abc.ABC,), {
        "__module__": "app.domain", "name": abc.abstractmethod(lambda self: None)})
    classes = []
    for name in names or ("MemRepo", "SqlRepo"):
        classes.append(type(name, (contract,), {
            "__module__": "app.infrastructure", "name": lambda self, n=name: n}))
    return contract, *classes


def _patch(container, *classes, monkeypatch, cache=True):
    name_map = autowired._build_name_map_from_container(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE",
                        autowired._CachedNameMap(container) if cache else None)
    for cls in classes:
        autowired._patch_setattr(cls, container, name_map)
        autowired._patch_init(cls, container, name_map)
    return classes[0] if len(classes) == 1 else classes


def _consumer(*fields, assign=True):
    """A consumer that sets the fields in __init__ (assign) or as class attributes."""
    if assign:
        body = "".join(f"        self.{name} = None\n" for name in fields) or "        pass\n"
        namespace = {}
        exec("class Consumer:\n    __module__ = 'app.application'\n"
             "    def __init__(self):\n" + body, namespace)
        return namespace["Consumer"]
    return type("Consumer", (), {"__module__": "app.application", **{n: None for n in fields}})


# ---- field names: upper and lower case -----------------------------------------------
FORMS = ["sql_repo_instance", "sqlrepo_instance", "sqlRepo_instance", "SqlRepo_instance"]


@pytest.mark.parametrize("assign", [True, False], ids=["self-assigned", "class-attribute"])
@pytest.mark.parametrize("field", FORMS)
def test_every_spelling_of_the_class_name_is_injected(field, assign, monkeypatch):
    contract, sql = _family("IRepo", "SqlRepo")
    container = Container()
    container.register_scoped(contract, sql)
    consumer = _patch(container, _consumer(field, assign=assign), monkeypatch=monkeypatch)
    with container.create_scope():
        assert getattr(consumer(), field).name() == "SqlRepo"


@pytest.mark.parametrize("field", ["product_repository_instance", "productRepository_instance",
                                   "ProductRepository_instance", "productrepository_instance"])
def test_every_spelling_of_the_contract_name_is_injected(field, monkeypatch):
    contract, memory = _family("IProductRepository", "MemoryProductRepository")
    container = Container()
    container.register_singleton(contract, memory)
    consumer = _patch(container, _consumer(field), monkeypatch=monkeypatch)
    assert getattr(consumer(), field).name() == "MemoryProductRepository"


def test_annotated_camel_case_field_is_a_candidate():
    class Consumer:
        sqlRepo_instance: object = None
        repoItems_instances: list = None

    assert set(autowired._find_di_candidates(Consumer)) == {"sqlRepo_instance", "repoItems_instances"}


@pytest.mark.parametrize("name,expected", [
    ("sql_repo_instance", True), ("SqlRepo_instance", True), ("sqlRepo_instance", True),
    ("repo_instances", True), ("Repo_instances", True), ("x_instance", True),
    ("_sql_repo_instance", False), ("9repo_instance", False), ("repo_instancex", False),
    ("repo_Instance", False), ("instance", False), ("repo-x_instance", False),
])
def test_field_pattern(name, expected):
    assert bool(autowired._DEF_ATTR_RE.match(name)) is expected


@pytest.mark.parametrize("name,key", [
    ("sql_repo_instance", "sqlrepo"), ("SqlRepo_instance", "sqlrepo"),
    ("sqlRepo_instance", "sqlrepo"), ("repo_instances", ("*", "repo")),
    ("Product_Repository_instances", ("*", "productrepository")),
])
def test_field_key(name, key):
    assert autowired._field_key(name) == key


# ---- container: several registrations of one contract --------------------------------
def test_resolve_keeps_the_last_registration_and_resolve_all_returns_every_one():
    contract, memory, sql = _family()
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    assert type(container.resolve(contract)) is sql
    assert [type(item) for item in container.resolve_all(contract)] == [memory, sql]


def test_each_registration_keeps_its_own_lifetime():
    contract, memory, sql, file = _family("IRepo", "MemRepo", "SqlRepo", "FileRepo")
    container = Container()
    container.register_singleton(contract, memory)
    container.register_scoped(contract, sql)
    container.register_transient(contract, file)
    with container.create_scope() as scope:
        first, second = scope.resolve_all(contract), scope.resolve_all(contract)
        assert first[0] is second[0] and first[1] is second[1] and first[2] is not second[2]
        assert container.resolve(contract) is not first[2]          # transient, the current one
    with container.create_scope():
        third = container.resolve_all(contract)
    assert third[0] is first[0] and third[1] is not first[1]       # singleton kept, new scope


def test_earlier_scoped_registration_has_its_own_scope_slot():
    contract, memory, sql = _family()
    container = Container()
    container.register_scoped(contract, memory)
    container.register_scoped(contract, sql)
    earlier = container._earlier[contract][0]
    assert earlier.slot != contract and container.registrations[contract].slot is contract
    assert core._slot_name(earlier.slot) == "IRepo[MemRepo]"
    with container.create_scope():
        memory_object, sql_object = container.resolve_all(contract)
        assert type(memory_object) is memory and type(sql_object) is sql
        assert container.resolve(contract) is sql_object


def test_registering_the_same_class_again_moves_it_to_the_end():
    contract, memory, sql = _family()
    container = Container()
    container.register_scoped(contract, memory)
    container.register_scoped(contract, sql)
    container.register_singleton(contract, memory)
    assert type(container.resolve(contract)) is memory
    with container.create_scope():
        assert [type(item) for item in container.resolve_all(contract)] == [sql, memory]
    container.register_singleton(contract, memory)                   # exact repeat
    assert [item.implementation_type for item in container._earlier[contract]] == [sql]


def test_replace_drops_every_earlier_registration():
    contract, memory, sql, file = _family("IRepo", "MemRepo", "SqlRepo", "FileRepo")
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    container.replace(contract, file)
    assert [type(item) for item in container.resolve_all(contract)] == [file]
    assert contract not in container._earlier


def test_fallback_and_strict_registrations_are_unchanged():
    contract, memory, sql = _family()
    container = Container()
    container.register_transient(contract, memory)
    assert container.register_fallback(contract, sql) is False
    assert [type(item) for item in container.resolve_all(contract)] == [memory]
    strict = Container(strict_registrations=True)
    strict.register_transient(contract, memory)
    with pytest.raises(DuplicateServiceRegistrationError):
        strict.register_transient(contract, sql)
    assert contract not in strict._earlier


def test_factories_and_instances_are_all_kept():
    contract, memory, sql = _family()
    first, second = memory(), sql()
    container = Container()
    container.register_instance(contract, first)
    container.register_instance(contract, second)
    assert container.resolve_all(contract) == [first, second]
    twins = Container()
    one, two = memory(), memory()
    twins.register_instance(contract, one)
    twins.register_instance(contract, two)
    assert twins.resolve_all(contract) == [one, two]                 # supplied objects, both kept
    factories = Container()
    factories.register_factory(contract, lambda c: first)
    factories.register_factory(contract, lambda c: second)
    assert factories.resolve_all(contract) == [first, second]


def test_an_alias_is_not_kept_as_an_earlier_registration():
    contract, memory, sql = _family()
    alias = type("IAlias", (abc.ABC,), {})
    container = Container()
    container.register_transient(contract, memory)
    container.register_alias(alias, contract)
    container.register_transient(alias, sql)
    assert alias not in container._earlier
    assert container.registrations[contract].variant is core._UNSET
    assert [type(item) for item in container.resolve_all(contract)] == [memory]


def test_an_interface_registration_without_implementation_is_not_kept():
    contract = type("IEmpty", (abc.ABC,), {"run": abc.abstractmethod(lambda self: None)})
    container = Container()
    container.register_transient(contract)                           # nothing imported yet
    implementation = type("EmptyImpl", (contract,), {"run": lambda self: 1})
    container.register_transient(contract, implementation)
    assert contract not in container._earlier
    assert [type(item) for item in container.resolve_all(contract)] == [implementation]


def test_completing_an_interface_registration_drops_its_earlier_duplicate():
    contract = type("ILate", (abc.ABC,), {"run": abc.abstractmethod(lambda self: None)})
    other = type("Other", (), {"run": lambda self: 0})
    late = type("Late", (contract,), {"run": lambda self: 1})
    container = Container()
    container.register_transient(contract, other)
    container.register_transient(contract, late)
    container._store_registration(core.ServiceRegistration(service_type=contract))   # no implementation
    assert [item.implementation_type for item in container._earlier[contract]] == [other, late]
    container._complete_implementations()
    assert [item.implementation_type for item in container._earlier[contract]] == [other]
    assert [type(item) for item in container.resolve_all(contract)] == [other, late]
    only = Container()
    only.register_transient(contract, late)
    only._store_registration(core.ServiceRegistration(service_type=contract))
    only._complete_implementations()
    assert contract not in only._earlier
    assert [type(item) for item in only.resolve_all(contract)] == [late]


def test_resolve_all_of_an_unknown_type_is_empty_and_open_generics_count():
    contract, memory, _ = _family()
    container = Container()
    assert container.resolve_all(contract) == []

    from typing import Generic, TypeVar
    T = TypeVar("T")

    class Box(Generic[T]):
        pass

    box = Box
    container.register_open_generic(box)
    assert [type(item) for item in container.resolve_all(box[int])] == [box]


def test_shutdown_releases_and_closes_earlier_singletons():
    closed = []
    contract, memory, sql = _family()
    memory.close = lambda self: closed.append("memory")
    sql.close = lambda self: closed.append("sql")
    container = Container()
    container.register_singleton(contract, memory)
    container.register_singleton(contract, sql)
    first = container.resolve_all(contract)
    container._release_singletons()
    assert closed == ["sql", "memory"]
    second = container.resolve_all(contract)
    assert second[0] is not first[0] and second[1] is not first[1]
    container.close()
    assert closed == ["sql", "memory", "sql", "memory"]


# ---- field injection -----------------------------------------------------------------
@pytest.mark.parametrize("cache", [True, False], ids=["cached", "uncached"])
def test_fields_by_class_name_by_contract_and_plural(cache, monkeypatch):
    contract, memory, sql = _family()
    container = Container()
    container.register_scoped(contract, memory)
    container.register_scoped(contract, sql)
    consumer = _patch(container, _consumer(
        "mem_repo_instance", "memRepo_instance", "MemRepo_instance", "sql_repo_instance",
        "repo_instance", "repo_instances", "irepo_instances"), monkeypatch=monkeypatch, cache=cache)
    with container.create_scope():
        made = consumer()
        assert made.mem_repo_instance.name() == "MemRepo"
        assert made.memRepo_instance is made.mem_repo_instance is made.MemRepo_instance
        assert made.sql_repo_instance.name() == "SqlRepo"
        assert made.repo_instance is made.sql_repo_instance           # last registration
        assert made.repo_instances == [made.mem_repo_instance, made.sql_repo_instance]
        assert made.irepo_instances == made.repo_instances


def test_plural_field_with_one_registration_and_without_a_match(monkeypatch):
    contract, memory = _family("IRepo", "MemRepo")
    container = Container()
    container.register_transient(contract, memory)
    consumer = _patch(container, _consumer("repo_instances", "unknown_instances", "mem_repo_instances"),
                      monkeypatch=monkeypatch)
    made = consumer()
    assert [type(item) for item in made.repo_instances] == [memory]
    assert made.unknown_instances is None and made.mem_repo_instances is None


@pytest.mark.parametrize("contract_name", ["ABS_Repo", "AbsRepo", "I_Repo", "RepoImpl", "abs_Repo"])
def test_plural_field_finds_prefixed_contracts(contract_name, monkeypatch):
    contract, memory, sql = _family(contract_name, "MemRepo", "SqlRepo")
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    consumer = _patch(container, _consumer("repo_instances"), monkeypatch=monkeypatch)
    assert [type(item) for item in consumer().repo_instances] == [memory, sql]


def test_earlier_class_named_like_the_contract_does_not_take_its_field(monkeypatch):
    contract, plain, sql = _family("IRepo", "Repo", "SqlRepo")
    container = Container()
    container.register_transient(contract, plain)
    container.register_transient(contract, sql)
    consumer = _patch(container, _consumer("repo_instance", "repo_instances"), monkeypatch=monkeypatch)
    made = consumer()
    assert type(made.repo_instance) is sql
    assert [type(item) for item in made.repo_instances] == [plain, sql]


def test_earlier_implementation_is_found_by_its_generic_name_too(monkeypatch):
    contract, memory, sql = _family("IRepo", "MemRepoImpl", "SqlRepo")
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    consumer = _patch(container, _consumer("mem_repo_instance", "mem_repo_impl_instance"),
                      monkeypatch=monkeypatch)
    made = consumer()
    assert type(made.mem_repo_instance) is memory and type(made.mem_repo_impl_instance) is memory
    assert None not in autowired._build_name_map_from_container(container)


def test_plural_field_through_an_alias(monkeypatch):
    contract, memory, sql = _family()
    alias = type("IStore", (abc.ABC,), {})
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    container.register_alias(alias, contract)
    consumer = _patch(container, _consumer("store_instances"), monkeypatch=monkeypatch)
    assert [type(item) for item in consumer().store_instances] == [memory, sql]


def test_failing_earlier_registration_is_reported_with_the_field(monkeypatch):
    contract, memory, sql = _family()

    def broken(self):
        raise RuntimeError("boom")

    memory.__init__ = broken
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    consumer = _patch(container, _consumer("mem_repo_instance"), monkeypatch=monkeypatch)
    with pytest.raises(AutoInjectionResolutionError, match="mem_repo_instance"):
        consumer()
    plural = _patch(container, _consumer("repo_instances"), monkeypatch=monkeypatch)
    with pytest.raises(AutoInjectionResolutionError, match="repo_instances"):
        plural()


def test_validation_accepts_class_name_and_plural_fields(monkeypatch):
    contract, memory, sql = _family()
    container = Container()
    container.register_scoped(contract, memory)
    container.register_scoped(contract, sql)
    _patch(container, _consumer("mem_repo_instance", "sqlRepo_instance", "repo_instances"),
           monkeypatch=monkeypatch)
    validate_autowiring(container)


def test_validation_checks_every_registration_behind_a_plural_field(monkeypatch):
    contract, memory, sql = _family()
    missing = type("Missing", (), {})

    def needs(self, dependency: missing):
        pass

    memory.__init__ = needs
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    _patch(container, _consumer("repo_instances"), monkeypatch=monkeypatch)
    with pytest.raises(AutowiringValidationError) as caught:
        validate_autowiring(container)
    assert any("repo_instances" in issue and "Missing" in issue for issue in caught.value.issues)
    single = Container()
    single.register_transient(contract, memory)
    single.register_transient(contract, sql)
    _patch(single, _consumer("repo_instance"), monkeypatch=monkeypatch)
    validate_autowiring(single)                                       # only SqlRepo is used


def test_validation_follows_plural_fields_inside_a_dependency(monkeypatch):
    contract, memory, sql = _family()
    missing = type("Missing", (), {})

    def needs(self, dependency: missing):
        pass

    memory.__init__ = needs
    holder = _consumer("repo_instances")
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    container.register_transient(holder)
    _patch(container, holder, _consumer("consumer_instance"), monkeypatch=monkeypatch)
    with pytest.raises(AutowiringValidationError) as caught:
        validate_autowiring(container)
    assert any("consumer_instance" in issue and "Missing" in issue for issue in caught.value.issues)


def test_validation_sees_an_earlier_singleton_capturing_a_scoped_service(monkeypatch):
    clock, system_clock = _family("IClock", "SystemClock")
    contract, memory, sql = _family()
    memory_with_clock = _patch(Container(), _consumer("clock_instance"), monkeypatch=monkeypatch)
    memory_with_clock.__name__ = "MemRepo"
    memory_with_clock.name = lambda self: "MemRepo"
    contract.register(memory_with_clock)
    container = Container()
    container.register_scoped(clock, system_clock)
    container.register_singleton(contract, memory_with_clock)
    container.register_singleton(contract, sql)
    _patch(container, memory_with_clock, monkeypatch=monkeypatch)
    with pytest.raises(AutowiringValidationError) as caught:
        validate_autowiring(container)
    assert any("captures scoped" in issue for issue in caught.value.issues)


# ---- a real project ------------------------------------------------------------------
FILES = {
    "domain/repo.py": (
        "import abc\n"
        "class IRepo(abc.ABC):\n"
        "    @abc.abstractmethod\n"
        "    def name(self): ...\n"),
    "infrastructure/deep/stores/mem_repo.py": (
        "from domain.repo import IRepo\n"
        "class MemRepo(IRepo):\n"
        "    def name(self):\n"
        "        return 'mem'\n"),
    "infrastructure/sql_repo.py": (
        "from domain.repo import IRepo\n"
        "class SqlRepo(IRepo):\n"
        "    def name(self):\n"
        "        return 'sql'\n"),
    "application/report.py": (
        "class Report:\n"
        "    def __init__(self):\n"
        "        self.memRepo_instance = None\n"
        "        self.SqlRepo_instance = None\n"
        "        self.repo_instance = None\n"
        "        self.repo_instances = None\n"
        "    def names(self):\n"
        "        return [self.memRepo_instance.name(), self.SqlRepo_instance.name(),\n"
        "                self.repo_instance.name(), [r.name() for r in self.repo_instances]]\n"),
    "bootstrap/composition.py": (
        "from py_autowired import Container\n"
        "from domain.repo import IRepo\n"
        "from infrastructure.deep.stores.mem_repo import MemRepo\n"
        "from infrastructure.sql_repo import SqlRepo\n"
        "container = Container(strict_interfaces=True)\n"
        "container.register_scoped(IRepo, MemRepo)\n"
        "container.register_scoped(IRepo, SqlRepo)\n"),
}
LAYERS = ("domain", "infrastructure", "application", "bootstrap")


def _forget():
    for name in [n for n in sys.modules if n.split(".")[0] in LAYERS]:
        sys.modules.pop(name, None)


def test_real_project_with_two_implementations(tmp_path, monkeypatch):
    root = tmp_path / "shop"
    for rel, body in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    (root / "requirements.txt").write_text("")
    _forget()
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    try:
        composition = importlib.import_module("bootstrap.composition")
        autowired.auto_inject(composition.container, root_dir=str(root))
        report = importlib.import_module("application.report")
        with composition.container.create_scope():
            made = report.Report()
            assert made.names() == ["mem", "sql", "sql", ["mem", "sql"]]
            assert made.repo_instances[0] is made.memRepo_instance
        validate_autowiring(composition.container)
    finally:
        _forget()


def test_unknown_service_message_is_unchanged():
    contract, _, _ = _family()
    with pytest.raises(ServiceNotRegisteredError):
        Container().resolve(contract)


# ---- the guides document all of it ---------------------------------------------------
@pytest.mark.parametrize("guide,heading", [
    ("README.md", "### Several implementations of one contract"),
    ("KULLANIM_KILAVUZU_TR.md", "### Bir sözleşmenin birden çok uygulaması"),
])
def test_guides_document_spellings_and_several_implementations(guide, heading):
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[1] / guide).read_text(encoding="utf-8")
    for spelling in ("sql_product_repository_instance", "sqlproductrepository_instance",
                     "sqlProductRepository_instance", "SqlProductRepository_instance",
                     "product_repository_instances", "resolve_all"):
        assert spelling in text, spelling
    assert heading in text


# ---- details found by mutation testing -----------------------------------------------
def test_name_map_has_no_empty_key_for_earlier_registrations():
    contract, memory, sql = _family()
    container = Container()
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    name_map = autowired._build_name_map_from_container(container)
    assert None not in name_map
    assert [reg.implementation_type for reg in name_map["memrepo"]] == [memory]


def test_earlier_factory_without_implementation_is_listed_but_not_named(monkeypatch):
    contract, memory, sql = _family()
    made = memory()
    container = Container()
    container._store_registration(core.ServiceRegistration(service_type=contract, factory=lambda c: made))
    container.register_transient(contract, sql)
    name_map = autowired._build_name_map_from_container(container)
    assert all(reg.factory is None for regs in name_map.values() for reg in regs
               if reg is not container.registrations[contract])
    assert container.resolve_all(contract)[0] is made


@pytest.mark.parametrize("name", ["ABS_A", "Abs_A", "abs_A", "I_A", "IA", "AbsA", "AImpl"])
def test_one_letter_after_the_prefix_is_still_a_name(name):
    assert autowired._generic_key(name) == "a"


def test_an_earlier_factory_does_not_hide_the_classes_registered_after_it(monkeypatch):
    contract, memory, sql = _family()
    container = Container()
    container.register_factory(contract, lambda c: sql())
    container.register_transient(contract, memory)
    container.register_transient(contract, sql)
    consumer = _patch(container, _consumer("mem_repo_instance"), monkeypatch=monkeypatch)
    assert type(consumer().mem_repo_instance) is memory
    assert [type(item) for item in container.resolve_all(contract)] == [sql, memory, sql]
