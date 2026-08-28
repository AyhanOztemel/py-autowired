from pathlib import Path
import importlib.util
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _environment():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    return environment


def _run(arguments):
    return subprocess.run(
        [sys.executable, "-B", *arguments],
        cwd=ROOT,
        env=_environment(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_runtime_package_has_only_two_python_files():
    files = sorted(path.name for path in (ROOT / "src" / "py_autowired").glob("*.py"))
    assert files == ["autowired.py", "container.py"]


def test_console_direct_source_run_needs_no_pythonpath_src():
    result = _run([str(ROOT / "examples" / "console_app" / "main.py")])
    assert result.returncode == 0, result.stderr
    assert "Runtime DI calisiyor" in result.stdout
    assert "AUTO_INJECT DERINLIK ZINCIRI" in result.stdout
    assert "MemoryMessageRepository.message_for()" in result.stdout
    assert "RuntimeLabelProvider.label()" in result.stdout


@pytest.mark.parametrize(
    ("module_name", "probe"),
    [
        (
            "fastapi",
            "from fastapi.testclient import TestClient;"
            "from examples.fastapi_app.app import app;"
            "client=TestClient(app);client.__enter__();"
            "home=client.get('/');assert home.status_code==200;assert '/di-demo/Ayhan' in home.text;response=client.get('/di-demo/FastAPI');legacy=client.get('/hello/FastAPI');assert legacy.status_code==200;"
            "assert response.status_code==200;"
            "assert 'FastAPI' in response.json()['message'];assert len(response.json()['injection_trace'])==5;assert max(step['module_depth'] for step in response.json()['injection_trace'])>=8;"
            "client.__exit__(None,None,None)",
        ),
        (
            "flask",
            "from examples.flask_app.app import app;"
            "client=app.test_client();home=client.get('/');assert home.status_code==200;assert '/di-demo/Ayhan' in home.get_data(as_text=True);response=client.get('/di-demo/Flask');legacy=client.get('/hello/Flask');assert legacy.status_code==200;"
            "assert response.status_code==200;"
            "assert 'Flask' in response.get_json()['message'];assert len(response.get_json()['injection_trace'])==5;assert max(step['module_depth'] for step in response.get_json()['injection_trace'])>=8",
        ),
        (
            "django",
            "import os;os.environ.setdefault("
            "'DJANGO_SETTINGS_MODULE','examples.django_app.runtime_di_demo.settings');"
            "import django;django.setup();"
            "from django.test import Client;"
            "client=Client();home=client.get('/');assert home.status_code==200;assert '/di-demo/Ayhan' in home.content.decode();response=client.get('/di-demo/Django');legacy=client.get('/hello/Django/');assert legacy.status_code==200;"
            "assert response.status_code==200;"
            "assert 'Django' in response.json()['message'];assert len(response.json()['injection_trace'])==5;assert max(step['module_depth'] for step in response.json()['injection_trace'])>=8",
        ),
    ],
)
def test_web_layered_examples_without_pythonpath_src(module_name, probe):
    if importlib.util.find_spec(module_name) is None:
        pytest.skip(f"optional dependency not installed: {module_name}")
    result = _run(["-c", probe])
    assert result.returncode == 0, result.stderr


def test_application_and_examples_do_not_resolve_manually():
    forbidden = ("container." + "resolve(", "provider" + "(", "provide" + "(")
    paths = list((ROOT / "examples").rglob("*.py"))
    paths += [ROOT / "KULLANIM_KILAVUZU_TR.md", ROOT / "USAGE_GUIDE_EN.md"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{token} found in {path}"

def test_all_examples_have_editor_run_entry_points():
    entry_points = [
        ROOT / "examples" / "console_app" / "main.py",
        ROOT / "examples" / "fastapi_app" / "app.py",
        ROOT / "examples" / "flask_app" / "app.py",
        ROOT / "examples" / "django_app" / "manage.py",
    ]
    for path in entry_points:
        text = path.read_text(encoding="utf-8")
        assert 'if __name__ == "__main__":' in text, path

    fastapi_text = entry_points[1].read_text(encoding="utf-8")
    flask_text = entry_points[2].read_text(encoding="utf-8")
    django_text = entry_points[3].read_text(encoding="utf-8")
    assert "uvicorn.run(app" in fastapi_text
    assert "use_reloader=False" in flask_text
    assert "len(arguments) == 1" in django_text

def test_deep_layered_runtime_injection_chain():
    probe = (
        "from examples.shared.bootstrap.dependency_injection.composition "
        "import configure_dependencies;"
        "from examples.shared.application.use_cases.greetings.greeting_service "
        "import GreetingService;"
        "from examples.shared.infrastructure.persistence.adapters.memory.repositories.message_repository "
        "import MemoryMessageRepository;"
        "from examples.shared.infrastructure.presentation.formatting.turkish_message_formatter "
        "import TurkishMessageFormatter;"
        "from examples.shared.infrastructure.runtime.environment.metadata.providers.runtime_label_provider "
        "import RuntimeLabelProvider;"
        "container=configure_dependencies();"
        "scope=container.create_scope();scope.__enter__();"
        "service=GreetingService();"
        "repository=service.message_repository_instance;"
        "formatter=repository.message_formatter_instance;"
        "label_provider=formatter.runtime_label_provider_instance;"
        "assert isinstance(repository,MemoryMessageRepository);"
        "assert isinstance(formatter,TurkishMessageFormatter);"
        "assert isinstance(label_provider,RuntimeLabelProvider);"
        "assert service.greet('Derinlik')=='Merhaba, Derinlik! Runtime DI calisiyor.';"
        "scope.__exit__(None,None,None)"
    )
    result = _run(["-c", probe])
    assert result.returncode == 0, result.stderr


def test_shared_example_is_structurally_deep_not_flat():
    shared = ROOT / "examples" / "shared"
    obsolete_flat_files = [
        shared / "application.py",
        shared / "composition.py",
        shared / "domain.py",
        shared / "infrastructure.py",
    ]
    assert not any(path.exists() for path in obsolete_flat_files)

    deep_files = [
        shared / "application" / "use_cases" / "greetings" / "greeting_service.py",
        shared / "infrastructure" / "persistence" / "adapters" / "memory" / "repositories" / "message_repository.py",
        shared / "infrastructure" / "runtime" / "environment" / "metadata" / "providers" / "runtime_label_provider.py",
        shared / "bootstrap" / "dependency_injection" / "composition.py",
    ]
    assert all(path.is_file() for path in deep_files)
    assert max(len(path.relative_to(shared).parts) for path in deep_files) >= 6
