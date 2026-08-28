from examples.shared.domain.contracts.messaging.message_repository import IMessageRepository


class MemoryMessageRepository(IMessageRepository):
    def __init__(self):
        self.message_formatter_instance = None

    def message_for(self, name: str) -> str:
        return self.message_formatter_instance.format_message(name)

    def injection_trace(self, name: str) -> list[dict[str, object]]:
        result = self.message_for(name)
        return [
            {
                "chain_level": 3,
                "module_depth": len(type(self).__module__.split(".")),
                "module": type(self).__module__,
                "class": type(self).__name__,
                "injected_field": "message_formatter_instance",
                "injected_type": type(self.message_formatter_instance).__name__,
                "method": "message_for()",
                "result": result,
                "instance_id": id(self),
            },
            *self.message_formatter_instance.injection_trace(name),
        ]
