"""0.5.1 late-import and prefix details found by mutation testing."""

import abc
import importlib
import logging
import sys
import threading
import uuid

import pytest

from py_autowired import Container, autowired


# ---- one-letter names right after a prefix ------------------------------------------
@pytest.mark.parametrize("name", ["I_A", "ABSA", "AbsA", "absA"])
def test_shortest_prefixed_names_still_match(name):
    contract = type(name, (abc.ABC,), {"run": abc.abstractmethod(lambda self: None)})
    container = Container()
    container.register_singleton(contract, type("Impl" + name, (contract,), {"run": lambda s: 1}))
    assert "a" in autowired._build_name_map_from_container(container)


# ---- a project with late-loaded modules ------------------------------------------------
@pytest.fixture
def late_project(tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"proj_{tag}"
    sibling = tmp_path / f"proj_{tag}x"                 # same text prefix, different folder
    for folder in (root, sibling):
        folder.mkdir()
    (root / f"clock_{tag}.py").write_text("class Clock:\n    pass\n")
    consumer = "class Consumer:\n    def __init__(self):\n        self.clock_instance = None\n"
    for name in ("first", "second", "threaded", "legacy", "failing"):
        (root / "dist").mkdir(exist_ok=True)
        (root / "dist" / f"{name}_{tag}.py").write_text(consumer)
    (sibling / f"neighbour_{tag}.py").write_text(consumer)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.syspath_prepend(str(root / "dist"))
    monkeypatch.syspath_prepend(str(sibling))
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    before = set(sys.modules)
    clock = importlib.import_module(f"clock_{tag}")
    container = Container()
    container.register_singleton(clock.Clock)
    autowired.auto_inject(container, root_dir=str(root))
    yield root, tag
    for name in set(sys.modules) - before:
        sys.modules.pop(name, None)


def _consumer(module_name):
    return importlib.import_module(module_name).Consumer()


def test_a_folder_that_only_shares_the_name_prefix_is_outside_the_project(late_project):
    _root, tag = late_project
    assert _consumer(f"neighbour_{tag}").clock_instance is None


def test_the_late_import_finder_runs_before_the_normal_finders(late_project):
    assert isinstance(sys.meta_path[0], autowired._LateImportFinder)


def test_late_import_in_another_thread_is_patched(late_project):
    _root, tag = late_project
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault("c", _consumer(f"threaded_{tag}")))
    worker.start()
    worker.join(10)
    assert box["c"].clock_instance is not None


def test_a_legacy_finder_without_find_spec_is_skipped(late_project, monkeypatch):
    _root, tag = late_project

    class LegacyFinder:                                  # only the old find_module API
        def find_module(self, fullname, path=None):
            return None

    monkeypatch.setattr(sys, "meta_path", [sys.meta_path[0], LegacyFinder(), *sys.meta_path[1:]])
    assert _consumer(f"legacy_{tag}").clock_instance is not None


def test_a_patching_failure_is_logged_and_the_import_still_works(late_project, monkeypatch, caplog):
    _root, tag = late_project

    def broken(*_a, **_k):
        raise RuntimeError("cannot patch")

    monkeypatch.setattr(autowired, "_patch_module_classes", broken)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        module = importlib.import_module(f"failing_{tag}")
    assert module.Consumer().clock_instance is None
    [record] = [r for r in caplog.records if "late-imported module" in r.getMessage()]
    assert isinstance(record.exc_info, tuple) and record.exc_info[0] is RuntimeError


def test_startup_scan_patches_each_module_once(late_project, monkeypatch):
    root, tag = late_project
    (root / f"added_{tag}.py").write_text(
        "class Added:\n    def __init__(self):\n        self.clock_instance = None\n")
    calls = []
    real = autowired._patch_module_classes

    def counting(module, *args):
        calls.append(module.__name__)
        return real(module, *args)

    monkeypatch.setattr(autowired, "_patch_module_classes", counting)
    autowired.auto_inject(autowired._NAME_MAP_CACHE._container, root_dir=str(root))
    assert calls.count(f"added_{tag}") == 1


@pytest.mark.parametrize("name", ["ABS", "Abs", "abs", "I_"])
def test_a_class_named_exactly_like_a_prefix_does_not_break_the_name_map(name):
    cls = type(name, (), {})
    container = Container()
    container.register_singleton(cls)
    assert autowired._norm(name) in autowired._build_name_map_from_container(container)
