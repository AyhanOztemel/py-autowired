"""Name-matching rules of runtime field injection (found untested by mutation
testing). Each convention strips exactly its prefix/suffix; near-misses do not."""

import pytest

from py_autowired import Container, autowired


def _wire(monkeypatch, container, consumer):
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    autowired._patch_setattr(consumer, container, cache.get_raw_map())
    autowired._patch_init(consumer, container, cache.get_raw_map())


def _consumer(field):
    return type("Consumer", (), {"__module__": "feature.consumer", field: None})


@pytest.mark.parametrize("class_name", [
    "ABS_Service", "Abs_Service", "abs_Service", "IService", "ServiceImpl", "Service",
])
def test_convention_maps_to_generic_field_name(monkeypatch, class_name):
    service = type(class_name, (), {"__module__": "feature.services"})
    container = Container()
    container.register_transient(service)
    consumer = _consumer("service_instance")
    _wire(monkeypatch, container, consumer)
    assert isinstance(consumer().service_instance, service)


@pytest.mark.parametrize("class_name, field", [
    ("Iservice", "service_instance"),     # 'I' + lowercase is not an interface prefix
    ("ABS_", "service_instance"),         # bare prefix has nothing to strip
    ("Impl", "service_instance"),         # bare suffix
    ("Abs_Service", "ervice_instance"),   # exactly the 4-char prefix is stripped
    ("ServiceImpl", "servic_instance"),   # exactly the 4-char suffix is stripped
])
def test_near_misses_do_not_match(monkeypatch, class_name, field):
    service = type(class_name, (), {"__module__": "feature.services"})
    container = Container()
    container.register_transient(service)
    consumer = _consumer(field)
    _wire(monkeypatch, container, consumer)
    assert getattr(consumer(), field) is None


def test_interface_and_implementation_names_both_match(monkeypatch):
    interface = type("IRepository", (), {"__module__": "feature.ports"})
    implementation = type("SqlRepositoryImpl", (interface,), {"__module__": "feature.adapters"})
    container = Container()
    container.register_transient(interface, implementation)
    consumer = type("Consumer", (), {
        "__module__": "feature.consumer",
        "repository_instance": None,        # IRepository -> repository
        "sql_repository_instance": None,    # SqlRepositoryImpl -> sqlrepository
        "sql_repository_impl_instance": None,
    })
    _wire(monkeypatch, container, consumer)
    obj = consumer()
    assert isinstance(obj.repository_instance, implementation)
    assert isinstance(obj.sql_repository_instance, implementation)
    assert isinstance(obj.sql_repository_impl_instance, implementation)


def test_same_module_beats_closer_package_beats_distant(monkeypatch):
    near = type("Clock", (), {"__module__": "app.billing.clock"})
    same = type("Clock", (), {"__module__": "app.billing.invoice"})
    far = type("Clock", (), {"__module__": "lib.time"})
    container = Container()
    container.register_transient(far)
    container.register_transient(near)
    container.register_transient(same)
    consumer = type("Invoice", (), {"__module__": "app.billing.invoice", "clock_instance": None})
    _wire(monkeypatch, container, consumer)
    assert type(consumer().clock_instance) is same

    container2 = Container()
    container2.register_transient(far)
    container2.register_transient(near)
    consumer2 = type("Invoice", (), {"__module__": "app.billing.invoice", "clock_instance": None})
    _wire(monkeypatch, container2, consumer2)
    assert type(consumer2().clock_instance) is near
