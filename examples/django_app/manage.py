import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "examples.django_app.runtime_di_demo.settings")

from django.core.management import execute_from_command_line


def main() -> None:
    arguments = sys.argv
    if len(arguments) == 1:
        arguments = [arguments[0], "runserver", "127.0.0.1:8103", "--noreload"]
        print("py-autowired: 5 katmanli Runtime DI demosu")
        print("Tarayicida acin: http://127.0.0.1:8103/di-demo/Ayhan")
        print("JSON cevabinda injection_trace alanini inceleyin.")
    execute_from_command_line(arguments)


if __name__ == "__main__":
    main()
