from examples.shared.domain.contracts.metadata.runtime_label_provider import IRuntimeLabelProvider


class RuntimeLabelProvider(IRuntimeLabelProvider):
    def label(self) -> str:
        return "Runtime DI"

    def injection_trace(self) -> list[dict[str, object]]:
        result = self.label()
        return [
            {
                "chain_level": 5,
                "module_depth": len(type(self).__module__.split(".")),
                "module": type(self).__module__,
                "class": type(self).__name__,
                "injected_field": None,
                "injected_type": None,
                "method": "label()",
                "result": result,
                "instance_id": id(self),
            }
        ]
