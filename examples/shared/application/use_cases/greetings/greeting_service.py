class GreetingService:
    def __init__(self):
        self.message_repository_instance = None

    def greet(self, name: str) -> str:
        return self.message_repository_instance.message_for(name)

    def injection_trace(self, name: str) -> list[dict[str, object]]:
        result = self.greet(name)
        return [
            {
                "chain_level": 2,
                "module_depth": len(type(self).__module__.split(".")),
                "module": type(self).__module__,
                "class": type(self).__name__,
                "injected_field": "message_repository_instance",
                "injected_type": type(self.message_repository_instance).__name__,
                "method": "greet()",
                "result": result,
                "instance_id": id(self),
            },
            *self.message_repository_instance.injection_trace(name),
        ]
