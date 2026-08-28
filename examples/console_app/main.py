import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from examples.shared.bootstrap.dependency_injection.composition import configure_dependencies


class ConsoleController:
    def __init__(self):
        self.greeting_service_instance = None

    def execute(self, name: str) -> dict[str, object]:
        message = self.greeting_service_instance.greet(name)
        controller_trace = {
            "chain_level": 1,
            "module_depth": len(type(self).__module__.split(".")),
            "module": type(self).__module__,
            "class": type(self).__name__,
            "injected_field": "greeting_service_instance",
            "injected_type": type(self.greeting_service_instance).__name__,
            "method": "execute()",
            "result": message,
            "instance_id": id(self),
        }
        return {
            "message": message,
            "injection_trace": [
                controller_trace,
                *self.greeting_service_instance.injection_trace(name),
            ],
        }


def main() -> None:
    container = configure_dependencies()
    with container.create_scope():
        result = ConsoleController().execute("Console")
        print(result["message"])
        print("\nAUTO_INJECT DERINLIK ZINCIRI")
        for step in result["injection_trace"]:
            print(
                f"[{step['chain_level']}] {step['module']} "
                f"(modul derinligi={step['module_depth']})"
            )
            print(
                f"    {step['class']}.{step['method']} -> {step['result']} "
                f"| inject: {step['injected_field']} = {step['injected_type']} "
                f"| instance_id={step['instance_id']}"
            )


if __name__ == "__main__":
    main()
