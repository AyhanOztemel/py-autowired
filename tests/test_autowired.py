from abc import ABC, abstractmethod
import asyncio
from pathlib import Path

from py_autowired.autowired import auto_inject
from py_autowired.container import Container


class IClock(ABC):
    @abstractmethod
    def now(self) -> str:
        raise NotImplementedError


class Clock(IClock):
    def now(self) -> str:
        return "now"


class IRepository(ABC):
    @abstractmethod
    def read(self) -> str:
        raise NotImplementedError


class Repository(IRepository):
    def __init__(self):
        self.clock_instance = None

    def read(self) -> str:
        return self.clock_instance.now()


class Service:
    def __init__(self):
        self.repository_instance = None

    def execute(self) -> str:
        return self.repository_instance.read()


class Controller:
    def __init__(self):
        self.service_instance = None


class ManualOverride:
    def __init__(self, service):
        self.service_instance = service


container = Container()
container.register_singleton(IClock, Clock)
container.register_scoped(IRepository, Repository)
container.register_transient(Service)
auto_inject(container, root_dir=str(Path(__file__).resolve().parents[1]))


def test_nested_runtime_injection_uses_instance_fields_only():
    with container.create_scope():
        controller = Controller()
        assert isinstance(controller.service_instance, Service)
        assert isinstance(controller.service_instance.repository_instance, Repository)
        assert isinstance(controller.service_instance.repository_instance.clock_instance, Clock)
        assert controller.service_instance.execute() == "now"


def test_lifetimes_are_preserved():
    with container.create_scope():
        first = Controller()
        second = Controller()
        assert first.service_instance is not second.service_instance
        assert (
            first.service_instance.repository_instance
            is second.service_instance.repository_instance
        )
        first_repository = first.service_instance.repository_instance
    with container.create_scope():
        third = Controller()
        assert third.service_instance.repository_instance is not first_repository


def test_non_none_manual_assignment_is_not_overwritten():
    marker = object()
    assert ManualOverride(marker).service_instance is marker


def test_async_scope_works():
    async def scenario():
        async with container.create_scope():
            assert Controller().service_instance.execute() == "now"

    asyncio.run(scenario())
