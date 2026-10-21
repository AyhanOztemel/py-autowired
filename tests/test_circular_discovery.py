"""A project file that imports the module still being loaded is skipped quietly.

Real case: the application module starts discovery while it is itself being
imported; ``main.py`` does ``from app import app`` and cannot load yet.
That is not a broken file, so it must not produce a WARNING. Real failures
(missing package, syntax error, missing name in a finished module) still do.
"""

import importlib
import importlib.machinery
import logging
import sys
import textwrap
import uuid

import pytest

from py_autowired import autowired


def _project(tmp_path, monkeypatch, files):
    name = f"circ_{uuid.uuid4().hex[:8]}"
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    for file_name, body in files.items():
        (pkg / file_name).write_text(textwrap.dedent(body).replace("PKG", name))
    monkeypatch.syspath_prepend(str(tmp_path))
    yield_names = [name]
    return name, pkg, yield_names


def _app_module():
    # Discovery runs BEFORE ``app`` exists, exactly like a real bootstrap.
    return """
        from pathlib import Path
        from py_autowired import autowired
        autowired._discover_and_load_modules(Path(__file__).resolve().parent.parent)
        app = "ready"
    """


@pytest.fixture
def cleanup():
    names = []
    yield names
    for name in list(sys.modules):
        if any(name == n or name.startswith(n + ".") for n in names):
            sys.modules.pop(name, None)


@pytest.mark.parametrize(
    "entry",
    [
        "from PKG.app_mod import app\n",                    # ImportError (from-import)
        "import PKG.app_mod as a\nVALUE = a.app\n",         # AttributeError (attribute read)
    ],
    ids=["from_import", "attribute_read"],
)
def test_entry_point_importing_loading_app_is_not_a_warning(tmp_path, monkeypatch, caplog, cleanup, entry):
    name, _, _ = _project(tmp_path, monkeypatch, {"app_mod.py": _app_module(), "main.py": entry})
    cleanup.append(name)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        module = importlib.import_module(f"{name}.app_mod")
    assert module.app == "ready"
    warnings_ = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings_ == [], [r.getMessage() for r in warnings_]
    skipped = [r.getMessage() for r in caplog.records if "still being imported" in r.getMessage()]
    assert skipped == [f"Skipped {name}.main during discovery: it imports {name}.app_mod, which is still being imported; "
                       "it is checked when it loads"]
    # Once the application module has finished, the entry point imports normally.
    sys.modules.pop(f"{name}.main", None)
    assert importlib.import_module(f"{name}.main") is not None


@pytest.mark.parametrize(
    "body, text",
    [
        ("import does_not_exist_pkg_xyz\n", "does_not_exist_pkg_xyz"),     # missing package
        ("def broken(:\n", "Import failed for"),                           # syntax error
        ("from PKG.done_mod import nothing_here\n", "nothing_here"),       # missing name, module finished
        ("import PKG.done_mod as d\nX = d.nothing_here\n", "nothing_here"),  # missing attribute, module finished
        ("raise RuntimeError('boom at import')\n", "boom at import"),      # any other import-time error
    ],
    ids=["missing_package", "syntax_error", "missing_name", "missing_attribute", "runtime_error"],
)
def test_real_failures_still_warn(tmp_path, monkeypatch, caplog, cleanup, body, text):
    name, pkg, _ = _project(tmp_path, monkeypatch, {"done_mod.py": "VALUE = 1\n", "bad.py": body})
    cleanup.append(name)
    importlib.import_module(f"{name}.done_mod")          # fully imported, not initializing
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        assert autowired._import_module_safely(f"{name}.bad", str(pkg / "bad.py")) is False
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and warns[0].startswith(f"Import failed for {name}.bad") and text in warns[0]
    assert not any("still being imported" in r.getMessage() for r in caplog.records)


def test_message_alone_is_not_trusted(caplog):
    """A message that merely mentions a circular import is a real failure unless that module is loading."""
    err = ImportError("cannot import name 'x' from partially initialized module 'json' (most likely due to a circular import)")
    assert autowired._still_importing(err) is None          # json is fully imported
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        def fail(_):
            raise err
        original = autowired.importlib.import_module
        autowired.importlib.import_module = fail
        try:
            assert autowired._import_module_safely("fake_mod", "fake_mod.py") is False
        finally:
            autowired.importlib.import_module = original
    assert "Import failed for fake_mod" in caplog.text


def test_still_importing_reads_name_and_obj(monkeypatch):
    loading = type(sys)("loading_mod_for_test")
    loading.__spec__ = importlib.machinery.ModuleSpec("loading_mod_for_test", None)
    loading.__spec__._initializing = True
    monkeypatch.setitem(sys.modules, "loading_mod_for_test", loading)
    assert autowired._still_importing(ImportError("x", name="loading_mod_for_test")) == "loading_mod_for_test"
    attr = AttributeError("x")
    attr.obj = loading
    assert autowired._still_importing(attr) == "loading_mod_for_test"
    loading.__spec__._initializing = False                  # finished loading → real failure
    assert autowired._still_importing(ImportError("x", name="loading_mod_for_test")) is None
    attr2 = AttributeError("x")
    attr2.obj = loading
    assert autowired._still_importing(attr2) is None


def test_skipped_entry_point_still_gets_its_fields_when_it_loads_later(tmp_path, monkeypatch, caplog):
    """Nothing is lost: the entry point is patched by the late-import hook once it really loads.

    Full public path (auto_inject), like a real FastAPI app: the app module starts
    discovery while it is being imported; main.py re-exports the app AND has a
    consumer class. No WARNING at startup, and the consumer's field is filled.
    """
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"proj_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    (root / f"clock_{tag}.py").write_text("class Clock:\n    def now(self):\n        return 'tick'\n")
    (root / f"app_{tag}.py").write_text(textwrap.dedent(f"""
        from pathlib import Path
        from py_autowired import Container, autowired
        from clock_{tag} import Clock
        container = Container()
        container.register_singleton(Clock)
        autowired.auto_inject(container, root_dir=str(Path(__file__).resolve().parent))
        app = "ready"
    """))
    (root / f"main_{tag}.py").write_text(textwrap.dedent(f"""
        from app_{tag} import app

        class Consumer:
            def __init__(self):
                self.clock_instance = None
    """))
    monkeypatch.syspath_prepend(str(root))
    before = set(sys.modules)
    try:
        with caplog.at_level(logging.DEBUG, logger="py_autowired"):
            importlib.import_module(f"app_{tag}")
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert any(f"Skipped main_{tag} during discovery" in r.getMessage() for r in caplog.records)
        main = importlib.import_module(f"main_{tag}")        # what the server does after startup
        consumer = main.Consumer()
        assert consumer.clock_instance is not None and consumer.clock_instance.now() == "tick"
    finally:
        for name in set(sys.modules) - before:
            if name.endswith(tag):
                sys.modules.pop(name, None)


def test_module_without_spec_is_a_real_failure(monkeypatch):
    """A module object with no __spec__ (e.g. created by hand) is never treated as 'still loading'."""
    bare = type(sys)("bare_mod_for_test")
    bare.__spec__ = None
    monkeypatch.setitem(sys.modules, "bare_mod_for_test", bare)
    assert autowired._still_importing(ImportError("x", name="bare_mod_for_test")) is None


def test_skipped_circular_module_is_not_counted_as_loaded(monkeypatch, caplog):
    loading = type(sys)("loading_mod_for_count")
    loading.__spec__ = importlib.machinery.ModuleSpec("loading_mod_for_count", None)
    loading.__spec__._initializing = True
    monkeypatch.setitem(sys.modules, "loading_mod_for_count", loading)

    def fail(_):
        raise ImportError("cannot import name 'app'", name="loading_mod_for_count")

    monkeypatch.setattr(autowired.importlib, "import_module", fail)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        assert autowired._import_module_safely("entry_mod", "entry_mod.py") is False
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
