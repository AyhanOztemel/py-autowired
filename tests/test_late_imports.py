"""0.5.1: no *_instance field is missed because of WHERE or WHEN a module loads.

Startup discovery skips some places on purpose (virtual environments, build
copies, ``tests``, ``_private.py`` files, ...). A project module from such a
place, or any project module imported only after startup, is now patched the
moment it is imported.
"""

import importlib
import sys
import threading
import uuid

import pytest

from py_autowired import Container, autowired


CASES = {
    "deep": "a/b/c/d/e/f/g/h/i/j/deep_consumer.py",
    "dist_folder": "dist/dist_consumer.py",
    "env_folder": "env/env_consumer.py",
    "underscore_file": "pkg/_private_consumer.py",
    "package_init": "init_pkg/__init__.py",
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"proj_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / f"clock_{tag}.py").write_text("class Clock:\n    def now(self):\n        return 'tick'\n")
    for case, rel in CASES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"class Consumer_{case}:\n"
            "    def __init__(self):\n"
            "        self.clock_instance = None\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    names = {case: rel[:-3].replace("/", ".").removesuffix(".__init__") for case, rel in CASES.items()}
    before = set(sys.modules)
    yield root, tag, names
    for name in set(sys.modules) - before:
        if any(name == n or name.startswith(n.split(".")[0] + ".") or name == n.split(".")[0]
               for n in names.values()) or name == f"clock_{tag}":
            sys.modules.pop(name, None)


def _start(root, tag):
    clock = importlib.import_module(f"clock_{tag}")
    container = Container()
    container.register_singleton(clock.Clock)
    autowired.auto_inject(container, root_dir=str(root))
    return container


@pytest.mark.parametrize("case", list(CASES))
def test_field_is_filled_wherever_and_whenever_the_module_loads(project, case):
    root, tag, names = project
    _start(root, tag)
    module = importlib.import_module(names[case])        # e.g. a lazy import at runtime
    consumer = getattr(module, f"Consumer_{case}")()
    assert consumer.clock_instance is not None
    assert consumer.clock_instance.now() == "tick"


def test_modules_outside_the_project_are_left_alone(project, tmp_path, monkeypatch):
    root, tag, _names = project
    _start(root, tag)
    outside = tmp_path / f"outside_{tag}"
    outside.mkdir()
    (outside / f"lib_{tag}.py").write_text(
        "class Library:\n    def __init__(self):\n        self.clock_instance = None\n")
    monkeypatch.syspath_prepend(str(outside))
    lib = importlib.import_module(f"lib_{tag}")
    try:
        assert lib.Library().clock_instance is None
        assert "__di_attrs__" not in lib.Library.__dict__
    finally:
        sys.modules.pop(f"lib_{tag}", None)


def test_late_import_while_another_thread_holds_the_lock_is_patched_afterwards(project):
    root, tag, names = project
    container = _start(root, tag)
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    try:
        module = importlib.import_module(names["dist_folder"])   # cannot take the lock now
    finally:
        release.set()
        holder.join(5)
    autowired.refresh()                                         # next lock holder catches up
    assert module.Consumer_dist_folder().clock_instance is not None
    assert autowired._NAME_MAP_CACHE._container is container
