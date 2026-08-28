from examples.shared.domain.contracts.formatting.message_formatter import IMessageFormatter


class TurkishMessageFormatter(IMessageFormatter):
    def __init__(self):
        self.runtime_label_provider_instance = None

    def format_message(self, name: str) -> str:
        runtime_label = self.runtime_label_provider_instance.label()
        return f"Merhaba, {name}! {runtime_label} calisiyor."

    def injection_trace(self, name: str) -> list[dict[str, object]]:
        result = self.format_message(name)
        return [
            {
                "chain_level": 4,
                "module_depth": len(type(self).__module__.split(".")),
                "module": type(self).__module__,
                "class": type(self).__name__,
                "injected_field": "runtime_label_provider_instance",
                "injected_type": type(self.runtime_label_provider_instance).__name__,
                "method": "format_message()",
                "result": result,
                "instance_id": id(self),
            },
            *self.runtime_label_provider_instance.injection_trace(),
        ]
