import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, jsonify
from examples.shared.bootstrap.dependency_injection.composition import configure_dependencies


DEMO_HOME_HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>py-autowired DI Demo</title></head>
<body><h1>py-autowired Runtime DI Demo</h1>
<p>Beş katmanlı auto-inject zincirini JSON olarak görmek için bağlantıyı açın.</p>
<p><a href="/di-demo/Ayhan">/di-demo/Ayhan</a></p></body></html>"""


class FlaskController:
    def __init__(self):
        self.greeting_service_instance = None

    def execute(self, name: str) -> dict[str, object]:
        message = self.greeting_service_instance.greet(name)
        return {
            "message": message,
            "injection_trace": [
                {
                    "chain_level": 1,
                    "module_depth": len(type(self).__module__.split(".")),
                    "module": type(self).__module__,
                    "class": type(self).__name__,
                    "injected_field": "greeting_service_instance",
                    "injected_type": type(self.greeting_service_instance).__name__,
                    "method": "execute()",
                    "result": message,
                    "instance_id": id(self),
                },
                *self.greeting_service_instance.injection_trace(name),
            ],
        }


app = Flask(__name__)
container = configure_dependencies()


@app.get("/")
def index():
    return DEMO_HOME_HTML


@app.get("/di-demo/<name>")
@app.get("/hello/<name>")
def di_demo(name: str):
    with container.create_scope():
        return jsonify(FlaskController().execute(name))


if __name__ == "__main__":
    print("py-autowired: 5 katmanli Runtime DI demosu")
    print("Tarayicida acin: http://127.0.0.1:8102/di-demo/Ayhan")
    print("JSON cevabinda injection_trace alanini inceleyin.")
    app.run(host="127.0.0.1", port=8102, debug=False, use_reloader=False)
