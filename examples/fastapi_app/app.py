import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from examples.shared.bootstrap.dependency_injection.composition import configure_dependencies
from py_autowired.fastapi import attach_container, injected


DEMO_HOME_HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>py-autowired DI Demo</title></head>
<body><h1>py-autowired Runtime DI Demo</h1>
<p>Beş katmanlı auto-inject zincirini JSON olarak görmek için bağlantıyı açın.</p>
<p><a href="/di-demo/World">/di-demo/World</a></p></body></html>"""


class FastApiController:
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


app = FastAPI()
attach_container(app, configure_dependencies(autowire=False), root_dir=str(PROJECT_ROOT))


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(DEMO_HOME_HTML)


@app.get("/di-demo/{name}")
async def di_demo(name: str):
    return FastApiController().execute(name)


@app.get("/hello/{name}", include_in_schema=False)
async def hello(name: str):
    return FastApiController().execute(name)


# FastAPI tarzı isteğe bağlı parametre enjeksiyonu.
@app.get("/di-demo-depends/{name}")
async def di_demo_depends(name: str, controller=Depends(injected(FastApiController))):
    return controller.execute(name)


if __name__ == "__main__":
    import uvicorn

    print("py-autowired: 5 katmanli Runtime DI demosu")
    print("Tarayicida acin: http://127.0.0.1:8101/di-demo/World")
    print("JSON cevabinda injection_trace alanini inceleyin.")
    uvicorn.run(app, host="127.0.0.1", port=8101)
