from django.http import HttpResponse, JsonResponse

from examples.shared.bootstrap.dependency_injection.composition import configure_dependencies


DEMO_HOME_HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>py-autowired DI Demo</title></head>
<body><h1>py-autowired Runtime DI Demo</h1>
<p>Beş katmanlı auto-inject zincirini JSON olarak görmek için bağlantıyı açın.</p>
<p><a href="/di-demo/Ayhan">/di-demo/Ayhan</a></p></body></html>"""


class DjangoController:
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


def index(_request):
    return HttpResponse(DEMO_HOME_HTML)


def di_demo(_request, name: str):
    container = configure_dependencies()
    with container.create_scope():
        return JsonResponse(DjangoController().execute(name))
