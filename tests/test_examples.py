"""The four examples are self-contained projects; each runs in its own process.

Every example has the same layers and a register/ folder that is the only place
importing the application classes. Consumers declare ``self.x_instance = None``.
"""

from pathlib import Path
import importlib.util
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
LAYER_FILES = [
    "domain/product_repository.py",
    "domain/clock.py",
    "domain/product_service.py",
    "infrastructure/memory_product_repository.py",
    "infrastructure/system_clock.py",
    "application/product_service.py",
    "presentation/product_controller.py",
    "register/register.py",
    "bootstrap/bootstrap.py",
    "requirements.txt",
    "README.md",
    "README.tr.md",
]
# Consumers never import other layers; an implementation may import only the
# contract it implements (from domain/).
CONSUMERS = {"application/product_service.py": ("from domain.",),
             "presentation/product_controller.py": ()}


def _run_in(example, code):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=EXAMPLES / example,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def test_runtime_package_stays_minimal():
    package = ROOT / "src" / "py_autowired"
    files = sorted(path.name for path in package.glob("*.py"))
    assert files == ["__init__.py", "_web.py", "autowired.py", "container.py", "django.py",
                     "fastapi.py", "flask.py"]
    assert (package / "py.typed").is_file()


def test_public_api_is_importable_from_the_package_root():
    import py_autowired
    from py_autowired import Container
    from py_autowired.container import Container as SubmoduleContainer

    assert Container is SubmoduleContainer
    assert not [name for name in py_autowired.__all__ if not hasattr(py_autowired, name)]


@pytest.mark.parametrize("example", ["console", "fastapi", "flask", "django"])
def test_every_example_is_complete_and_self_contained(example):
    base = EXAMPLES / example
    missing = [rel for rel in LAYER_FILES if not (base / rel).is_file()]
    assert not missing, missing
    assert "py_autowired" in (base / "requirements.txt").read_text(encoding="utf-8")
    for rel, allowed in CONSUMERS.items():
        text = (base / rel).read_text(encoding="utf-8")
        statements = [line.strip() for line in text.splitlines()]
        imports = [s for s in statements if s.startswith(("import ", "from "))]
        assert all(s.startswith(allowed) for s in imports) if allowed else not imports, (rel, imports)
        assert "_instance = None" in text, rel
    bootstrap = (base / "bootstrap" / "bootstrap.py").read_text(encoding="utf-8")
    assert "Container(strict_interfaces=True)" in bootstrap
    assert not (EXAMPLES / "shared").exists()


def test_examples_never_resolve_by_hand():
    forbidden = ("container." + "resolve(", "provider" + "(", "provide" + "(")
    for path in EXAMPLES.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{token} found in {path}"


def test_console_example():
    result = subprocess.run(
        [sys.executable, "-B", "main.py"],
        cwd=EXAMPLES / "console",
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "['Notebook', 'Pencil', 'Eraser']" in result.stdout


WEB_PROBES = {
    "fastapi": (
        "from fastapi.testclient import TestClient\n"
        "from main import app\n"
        "with TestClient(app) as client:\n"
        "    for _ in range(2):\n"
        "        body = client.get('/products').json()\n"
        "        assert body['products'] == ['Notebook', 'Pencil', 'Eraser'], body\n"),
    "flask": (
        "from app import app\n"
        "client = app.test_client()\n"
        "for _ in range(2):\n"
        "    response = client.get('/products')\n"
        "    assert response.get_json()['products'] == ['Notebook', 'Pencil', 'Eraser']\n"
        "    response.close()\n"),
    "django": (
        "import wsgiref.util\n"
        "from shop_site.wsgi import application\n"
        "for _ in range(2):\n"
        "    environ = {}\n"
        "    wsgiref.util.setup_testing_defaults(environ)\n"
        "    environ['PATH_INFO'] = '/products'\n"
        "    environ['HTTP_HOST'] = '127.0.0.1'\n"
        "    status = []\n"
        "    result = application(environ, lambda s, h, e=None: status.append(s))\n"
        "    body = b''.join(result)\n"
        "    result.close()\n"
        "    assert status[0].startswith('200'), status\n"
        "    assert b'Notebook' in body, body\n"),
}


@pytest.mark.parametrize("example", ["fastapi", "flask", "django"])
def test_web_example(example):
    if importlib.util.find_spec(example) is None:
        pytest.skip(f"optional dependency not installed: {example}")
    result = _run_in(example, WEB_PROBES[example])
    assert result.returncode == 0, result.stderr
