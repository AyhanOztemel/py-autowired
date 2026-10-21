"""0.5.4: a file skipped during discovery is visible and checked the moment it loads.

Discovery runs while the application module is still being imported, so a file
that imports it back (``main.py`` doing ``from app import app``) cannot load
yet. 0.5.3 logged that at DEBUG only and filled the file's fields when it
loaded later - but a wiring problem in that file then appeared only when the
field was used. Now:

* the skip is logged at INFO at startup;
* when the file loads, its ``*_instance`` fields are checked at once (ERROR, or
  AutowiringValidationError from the import when ``validate=True``);
* ``skipped_during_discovery()`` lists the skipped modules;
* ``validate=True`` names the skipped modules on a line of its own.
"""

import importlib
import logging
import sys
import textwrap
import threading
import uuid

import pytest

import py_autowired
from py_autowired import AutowiringValidationError, autowired


GOOD = """
class Consumer:
    def __init__(self):
        self.clock_instance = None
"""

MISSING = """
class Consumer:
    def __init__(self):
        self.missing_thing_instance = None
"""

UNRESOLVABLE = """
class Consumer:
    def __init__(self):
        self.broken_instance = None
"""


@pytest.fixture
def make_project(tmp_path, monkeypatch):
    """Build a project whose app module runs auto_inject while it is being imported."""
    tags = []

    def build(main_body, *, validate=False, extra=None):
        tag = uuid.uuid4().hex[:8]
        tags.append(tag)
        root = tmp_path / f"proj_{tag}"
        root.mkdir()
        (root / "requirements.txt").write_text("")
        (root / f"clock_{tag}.py").write_text(textwrap.dedent("""
            CREATED = []

            class Clock:
                def __init__(self):
                    CREATED.append(self)

                def now(self):
                    return "tick"

            class Needs:
                pass

            class Broken:
                def __init__(self, needs: Needs):
                    self.needs = needs
        """))
        (root / f"app_{tag}.py").write_text(textwrap.dedent(f"""
            from pathlib import Path
            from py_autowired import Container, autowired
            from clock_{tag} import Broken, Clock
            container = Container()
            container.register_singleton(Clock)
            container.register_singleton(Broken)
            autowired.auto_inject(container, root_dir=str(Path(__file__).resolve().parent), validate={validate})
            app = "ready"
        """))
        (root / f"main_{tag}.py").write_text(f"from app_{tag} import app\n" + textwrap.dedent(main_body).replace("TAG", tag))
        for name, body in (extra or {}).items():
            path = root / name.replace("TAG", tag)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(body).replace("TAG", tag))
        monkeypatch.syspath_prepend(str(root))
        return root, tag

    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    before = set(sys.modules)
    yield build
    for name in set(sys.modules) - before:
        if any(tag in name for tag in tags):
            sys.modules.pop(name, None)
    deferred = getattr(autowired, "_DEFERRED", {})
    for name in list(deferred):
        if any(tag in name for tag in tags):
            deferred.pop(name, None)


def _records(caplog, level, *parts):
    return [r.getMessage() for r in caplog.records
            if r.levelno == level and all(part in r.getMessage() for part in parts)]


def _import(name):
    """Import; return (module, None) or (None, error) - never lets the error escape."""
    try:
        return importlib.import_module(name), None
    except Exception as error:   # noqa: BLE001 - the test inspects it
        return None, error


# ---- 1. visible at startup -------------------------------------------------------------

def test_startup_reports_the_skipped_file_at_info(make_project, caplog):
    _root, tag = make_project(GOOD)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"app_{tag}")
    levels = [r.levelname for r in caplog.records
              if f"main_{tag}" in r.getMessage() and "still being imported" in r.getMessage()]
    assert levels == ["INFO"]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---- 2. checked the moment it loads ----------------------------------------------------

@pytest.mark.parametrize("body, problem", [
    (MISSING, "Consumer.missing_thing_instance: no matching registration"),
    (UNRESOLVABLE, "Consumer.broken_instance: registered but unresolvable"),
], ids=["no_registration", "unresolvable"])
def test_a_wiring_problem_is_an_error_as_soon_as_the_file_loads(make_project, caplog, body, problem):
    _root, tag = make_project(body)
    importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        module, error = _import(f"main_{tag}")
    assert error is None and module is not None          # validate=False: logged, not raised
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", problem)) == 1


def test_with_validate_the_import_of_the_file_fails(make_project, caplog):
    _root, tag = make_project(MISSING, validate=True)
    importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        module, error = _import(f"main_{tag}")
    assert module is None and isinstance(error, AutowiringValidationError)
    assert error.issues == (f"main_{tag}.Consumer.missing_thing_instance: no matching registration",)
    assert f"main_{tag}" not in sys.modules
    assert len(_records(caplog, logging.ERROR, f"main_{tag}")) == 1


def test_a_healthy_file_is_reported_as_checked(make_project, caplog):
    _root, tag = make_project(GOOD)
    importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        module = importlib.import_module(f"main_{tag}")
    assert len(_records(caplog, logging.INFO, "Checked", f"main_{tag}")) == 1
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert module.Consumer().clock_instance.now() == "tick"


def test_a_file_loaded_by_a_second_auto_inject_is_checked_too(make_project, caplog):
    root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(app.container, root_dir=str(root))   # its discovery loads main now
    assert f"main_{tag}" in sys.modules
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", "no matching registration")) == 1


def test_a_file_loaded_while_another_thread_holds_the_lock_is_checked_afterwards(make_project, caplog):
    _root, tag = make_project(MISSING)
    importlib.import_module(f"app_{tag}")
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    try:
        with caplog.at_level(logging.DEBUG, logger="py_autowired"):
            module = importlib.import_module(f"main_{tag}")      # queued: lock is busy
    finally:
        release.set()
        holder.join(5)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.refresh()                                     # next lock holder checks it
    assert module is not None
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", "no matching registration")) == 1


def test_validate_autowiring_checks_a_file_the_import_hook_did_not_see(make_project, monkeypatch):
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    with monkeypatch.context() as patch:                        # loaded around the hook
        patch.setattr(sys, "meta_path", [f for f in sys.meta_path
                                         if not isinstance(f, autowired._LateImportFinder)])
        importlib.import_module(f"main_{tag}")
    try:
        autowired.validate_autowiring(app.container)
        error = None
    except AutowiringValidationError as caught:
        error = caught
    assert error is not None
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in error.issues


def test_validate_autowiring_also_fills_a_file_the_import_hook_did_not_see(make_project, monkeypatch):
    _root, tag = make_project(GOOD)
    app = importlib.import_module(f"app_{tag}")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "meta_path", [f for f in sys.meta_path
                                         if not isinstance(f, autowired._LateImportFinder)])
        main = importlib.import_module(f"main_{tag}")
    autowired.validate_autowiring(app.container)
    assert main.Consumer().clock_instance is not None


# ---- 3. public list --------------------------------------------------------------------

def test_skipped_modules_can_be_read(make_project):
    root, tag = make_project(GOOD)
    read = getattr(py_autowired, "skipped_during_discovery", None)
    assert callable(read) and read is getattr(autowired, "skipped_during_discovery", None)
    assert "skipped_during_discovery" in py_autowired.__all__
    app = importlib.import_module(f"app_{tag}")
    assert read() == (f"main_{tag}",)
    importlib.import_module(f"main_{tag}")
    assert read() == (f"main_{tag}",)                     # the latest discovery's list
    autowired.auto_inject(app.container, root_dir=str(root))
    assert read() == ()                                   # nothing skipped this time


# ---- 4. validate=True output -----------------------------------------------------------

def test_validate_names_the_skipped_files_on_their_own_line(make_project, caplog):
    _root, tag = make_project(GOOD, validate=True, extra={"zz_TAG.py": "from app_TAG import app" + GOOD})
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"app_{tag}")
    assert _records(caplog, logging.INFO, "Validation") == [
        "Validation does not cover modules skipped during discovery; "
        f"they are checked when they load: main_{tag}, zz_{tag}"
    ]


def test_a_failed_validation_names_the_skipped_files_on_their_own_line(make_project):
    _root, tag = make_project(GOOD, validate=True, extra={"other_TAG.py": MISSING,
                                                          "zz_TAG.py": "from app_TAG import app" + GOOD})
    _module, error = _import(f"app_{tag}")
    assert isinstance(error, AutowiringValidationError)
    assert error.issues == (f"other_{tag}.Consumer.missing_thing_instance: no matching registration",)
    assert str(error).splitlines()[-1] == (
        f"Not checked yet (skipped during discovery; checked when they load): main_{tag}, zz_{tag}"
    )


def test_the_skipped_list_is_sorted_on_every_system(make_project):
    """Discovery reads the project folder before its subfolders; the list does not follow that order."""
    _root, tag = make_project(GOOD, extra={"aa_TAG/entry.py": "from app_TAG import app" + GOOD})
    importlib.import_module(f"app_{tag}")
    read = getattr(autowired, "skipped_during_discovery", lambda: None)
    assert read() == (f"aa_{tag}.entry", f"main_{tag}")


def test_a_hand_made_module_without_the_loading_flag_is_checked(make_project, monkeypatch):
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    hand_made = type(sys)(f"main_{tag}")
    hand_made.__spec__ = importlib.machinery.ModuleSpec(f"main_{tag}", None)   # no _initializing at all
    exec(compile(textwrap.dedent(MISSING), f"main_{tag}.py", "exec"), hand_made.__dict__)
    hand_made.Consumer.__module__ = f"main_{tag}"
    monkeypatch.setitem(sys.modules, f"main_{tag}", hand_made)
    try:
        autowired.validate_autowiring(app.container)
        error = None
    except AutowiringValidationError as caught:
        error = caught
    assert error is not None
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in error.issues


# ---- controls: unchanged behaviour (green before and after) -----------------------------

def test_control_a_real_failure_is_still_a_warning(make_project, caplog):
    _root, tag = make_project(GOOD, extra={"broken_TAG.py": "import does_not_exist_pkg_054\n"})
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"app_{tag}")
    assert len(_records(caplog, logging.WARNING, f"Import failed for broken_{tag}", "does_not_exist_pkg_054")) == 1
    assert _records(caplog, logging.INFO, f"broken_{tag}") == []


def test_control_the_check_constructs_nothing_and_fields_are_still_filled(make_project):
    _root, tag = make_project(GOOD)
    importlib.import_module(f"app_{tag}")
    clock = sys.modules[f"clock_{tag}"]
    main = importlib.import_module(f"main_{tag}")
    assert clock.CREATED == []                            # loading/checking built no service
    assert main.Consumer().clock_instance.now() == "tick"
    assert len(clock.CREATED) == 1


def test_control_validate_with_a_healthy_skipped_file_imports_normally(make_project):
    _root, tag = make_project(GOOD, validate=True)
    importlib.import_module(f"app_{tag}")
    module, error = _import(f"main_{tag}")
    assert error is None
    assert module.Consumer().clock_instance is not None


def test_control_a_lazily_imported_file_that_was_not_skipped_is_not_checked(make_project, caplog):
    _root, tag = make_project(GOOD, extra={"dist/lazy_TAG.py": MISSING})
    importlib.import_module(f"app_{tag}")
    sys.path.insert(0, str(_root / "dist"))
    try:
        with caplog.at_level(logging.DEBUG, logger="py_autowired"):
            lazy = importlib.import_module(f"lazy_{tag}")
    finally:
        sys.path.remove(str(_root / "dist"))
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert lazy.Consumer().missing_thing_instance is None   # unchanged 0.5.1 behaviour


def test_control_a_skipped_file_is_checked_once(make_project, caplog):
    _root, tag = make_project(MISSING)
    importlib.import_module(f"app_{tag}")
    importlib.import_module(f"main_{tag}")
    sys.modules.pop(f"main_{tag}")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"main_{tag}")               # loaded again: no second check
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


# ---- every route and branch of the check ------------------------------------------------

def test_a_later_auto_inject_raises_for_a_file_skipped_with_validate(make_project):
    root, tag = make_project(MISSING, validate=True)
    app = importlib.import_module(f"app_{tag}")
    try:
        autowired.auto_inject(app.container, root_dir=str(root))   # loads main; validate=True asked for it
        error = None
    except AutowiringValidationError as caught:
        error = caught
    assert error is not None
    assert error.issues == (f"main_{tag}.Consumer.missing_thing_instance: no matching registration",)


def test_the_first_failure_is_kept_when_several_skipped_files_load(make_project):
    root, tag = make_project(MISSING, validate=True,
                             extra={"zz_TAG.py": f"from app_TAG import app\n{GOOD}"})
    app = importlib.import_module(f"app_{tag}")
    assert set(py_autowired.__dict__.get("skipped_during_discovery", lambda: ())()) == {f"main_{tag}", f"zz_{tag}"}
    try:
        autowired.auto_inject(app.container, root_dir=str(root))   # main fails, zz is healthy
        error = None
    except AutowiringValidationError as caught:
        error = caught
    assert error is not None and f"main_{tag}" in str(error)


def test_a_patching_failure_of_a_skipped_file_is_an_error(make_project, monkeypatch, caplog):
    _root, tag = make_project(GOOD, validate=True)
    importlib.import_module(f"app_{tag}")
    real = autowired._patch_module_classes

    def broken(module, *args):
        if module.__name__ == f"main_{tag}":
            raise RuntimeError("cannot patch main")
        return real(module, *args)

    monkeypatch.setattr(autowired, "_patch_module_classes", broken)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        module, error = _import(f"main_{tag}")
    assert module is None and isinstance(error, AutowiringValidationError)
    assert error.issues == (f"main_{tag}: could not be checked: RuntimeError('cannot patch main')",)
    assert isinstance(error.__cause__, RuntimeError)
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", "could not be checked")) == 1
    assert _records(caplog, logging.INFO, "Checked") == []


def test_a_skipped_file_that_is_still_loading_is_not_checked_yet(make_project, monkeypatch):
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    loading = type(sys)(f"main_{tag}")
    loading.__spec__ = importlib.machinery.ModuleSpec(f"main_{tag}", None)
    loading.__spec__._initializing = True
    monkeypatch.setitem(sys.modules, f"main_{tag}", loading)
    autowired.validate_autowiring(app.container)            # must not check a half-loaded module
    assert f"main_{tag}" in getattr(autowired, "_DEFERRED", {})


def test_a_file_checked_by_validate_autowiring_keeps_working_without_the_cache(make_project, monkeypatch):
    _root, tag = make_project(GOOD)
    app = importlib.import_module(f"app_{tag}")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "meta_path", [f for f in sys.meta_path
                                         if not isinstance(f, autowired._LateImportFinder)])
        main = importlib.import_module(f"main_{tag}")
    autowired.validate_autowiring(app.container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)   # fall back to the patch's own map
    assert main.Consumer().clock_instance is not None


def test_nested_discovery_keeps_its_own_list(make_project, caplog):
    """An outer auto_inject whose discovery imports the app module (which runs its own)."""
    root, tag = make_project(MISSING)
    from py_autowired import Container
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(Container(), root_dir=str(root))   # imports app -> inner discovery skips main
    read = getattr(autowired, "skipped_during_discovery", lambda: None)
    assert read() == ()                                        # the outer discovery skipped nothing
    assert f"main_{tag}" in sys.modules                        # outer discovery loaded it afterwards
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", "no matching registration")) == 1


def test_only_the_files_own_classes_are_checked(make_project, caplog):
    _root, tag = make_project(f"from other_TAG import Other\n{MISSING}",
                              extra={"other_TAG.py": MISSING.replace("Consumer", "Other")})
    importlib.import_module(f"app_{tag}")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"main_{tag}")
    messages = _records(caplog, logging.ERROR, f"main_{tag}")
    assert len(messages) == 1 and f"other_{tag}" not in messages[0]


# ---- controls for the new bookkeeping ---------------------------------------------------

def test_control_discovery_leaves_no_open_list(make_project):
    _root, tag = make_project(GOOD)
    importlib.import_module(f"app_{tag}")
    assert getattr(autowired, "_DISCOVERING", []) == []


def test_control_without_skipped_files_validation_adds_nothing(tmp_path, caplog):
    root = tmp_path / f"plain_{uuid.uuid4().hex[:8]}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    from py_autowired import Container
    container = Container()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(container, root_dir=str(root), validate=True)
    assert _records(caplog, logging.INFO, "Validation") == []
    assert str(AutowiringValidationError(["x"])) == "Autowiring validation failed:\n- x"


def test_control_a_late_import_without_an_active_cache_does_not_fail(make_project, monkeypatch):
    _root, tag = make_project(GOOD, extra={"dist/later_TAG.py": GOOD})
    importlib.import_module(f"app_{tag}")
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    monkeypatch.syspath_prepend(str(_root / "dist"))
    module, error = _import(f"later_{tag}")
    assert error is None and module is not None
    monkeypatch.setattr(autowired, "_PENDING_LATE_MODULES", [])


def test_with_validate_every_later_import_of_the_broken_file_fails_too(make_project):
    _root, tag = make_project(MISSING, validate=True)
    importlib.import_module(f"app_{tag}")
    _first, first_error = _import(f"main_{tag}")
    module, error = _import(f"main_{tag}")                    # e.g. a retry: still broken
    assert isinstance(first_error, AutowiringValidationError)
    assert module is None and isinstance(error, AutowiringValidationError)
