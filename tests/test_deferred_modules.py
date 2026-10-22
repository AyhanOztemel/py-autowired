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
import time
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

    def build(main_body, *, validate=False, extra=None, app_tail=""):
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
            APP_TAIL
            app = "ready"
        """).replace("APP_TAIL", app_tail.replace("TAG", tag)))
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
    for state in ("_DEFERRED", "_AWAITING"):
        pending = getattr(autowired, state, {})
        for name in list(pending):
            if any(tag in name for tag in tags):
                pending.pop(name, None)


def _records(caplog, level, *parts):
    return [r.getMessage() for r in caplog.records
            if r.levelno == level and all(part in r.getMessage() for part in parts)]


def _call(function, *args):
    """Call; return the error instead of raising it."""
    try:
        function(*args)
    except Exception as error:   # noqa: BLE001 - the test inspects it
        return error
    return None


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


def _import_while_the_lock_is_busy(name, wait_for_drain=True):
    """The import hook cannot take the lock: the module is queued and its import succeeds.

    A background thread then patches and checks it as soon as the lock is free;
    by default this helper returns only after that has happened.
    """
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    try:
        return _import(name)
    finally:
        release.set()
        holder.join(5)
        if wait_for_drain:
            deadline = time.monotonic() + 5
            while autowired._PENDING_LATE_MODULES and time.monotonic() < deadline:
                time.sleep(0.01)
            with autowired._GLOBAL_LOCK:                     # the drain has finished its work
                pass


def test_a_queued_failure_with_validate_is_raised_by_the_next_auto_inject(make_project, caplog):
    root, tag = make_project(MISSING, validate=True)
    app = importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        module, error = _import_while_the_lock_is_busy(f"main_{tag}")
    assert error is None and module is not None             # the import finished; nobody to raise to
    # The background drain checked it at once: ERROR now, the raise is kept for the next call.
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", "no matching registration")) == 1
    try:
        autowired.auto_inject(app.container, root_dir=str(root))
        later = None
    except AutowiringValidationError as caught:
        later = caught
    assert later is not None
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in later.issues


def test_control_a_queued_failure_is_in_the_next_validate_autowiring(make_project):
    _root, tag = make_project(MISSING, validate=True)
    app = importlib.import_module(f"app_{tag}")
    _import_while_the_lock_is_busy(f"main_{tag}")
    _call(autowired.refresh)
    try:
        autowired.validate_autowiring(app.container)
        later = None
    except AutowiringValidationError as caught:
        later = caught
    assert later is not None
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in later.issues


# ---- refresh() / activate_container() finish their work, then raise ------------------------

def test_refresh_raises_a_queued_validate_failure_after_refreshing(make_project):
    _root, tag = make_project(MISSING, validate=True)
    app = importlib.import_module(f"app_{tag}")
    _import_while_the_lock_is_busy(f"main_{tag}")
    generation = autowired._NAME_MAP_CACHE._generation
    error = _call(autowired.refresh)
    assert isinstance(error, AutowiringValidationError)
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in error.issues
    assert autowired._NAME_MAP_CACHE._generation == generation + 1     # its own work was done
    assert autowired._NAME_MAP_CACHE._container is app.container


def test_activate_container_raises_a_queued_validate_failure_after_activating(make_project):
    from py_autowired import Container
    _root, tag = make_project(MISSING, validate=True)
    app = importlib.import_module(f"app_{tag}")
    _import_while_the_lock_is_busy(f"main_{tag}")
    other = Container()
    error = _call(autowired.activate_container, other)
    assert isinstance(error, AutowiringValidationError)
    assert autowired._NAME_MAP_CACHE._container is other              # its own work was done
    assert _call(autowired.activate_container, app.container) is None   # reported once, not on every call
    assert autowired._NAME_MAP_CACHE._container is app.container


def test_auto_inject_raises_a_failure_it_drains_after_finishing(make_project, monkeypatch, tmp_path):
    """The queue is drained once more after auto_inject() has done its work."""
    from py_autowired import Container
    _root, tag = make_project(MISSING, validate=True)
    importlib.import_module(f"app_{tag}")
    monkeypatch.setattr(autowired, "_drain_when_free", lambda: None, raising=False)   # keep it queued
    _import_while_the_lock_is_busy(f"main_{tag}", wait_for_drain=False)
    monkeypatch.setattr(autowired, "_check_loaded_deferred", lambda *a, **k: None, raising=False)
    empty = tmp_path / f"empty_{tag}"
    empty.mkdir()
    container = Container()
    error = _call(autowired.auto_inject, container, str(empty))
    assert isinstance(error, AutowiringValidationError)
    assert autowired._NAME_MAP_CACHE._container is container          # the call itself completed


def test_control_refresh_without_validate_only_logs(make_project, caplog):
    _root, tag = make_project(MISSING)
    importlib.import_module(f"app_{tag}")
    _import_while_the_lock_is_busy(f"main_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        assert _call(autowired.refresh) is None
    assert len(_records(caplog, logging.ERROR, f"main_{tag}")) == 1


# ---- a skipped file nobody imports is loaded and checked at the first safe moment ------------

def _first_injection(tag):
    """Any object with an injected field: the first injection after startup."""
    return sys.modules[f"other_{tag}"].Other()


OTHER = {"other_TAG.py": GOOD.replace("Consumer", "Other")}


@pytest.mark.parametrize("body, problem", [
    (MISSING, "Consumer.missing_thing_instance: no matching registration"),
    (UNRESOLVABLE, "Consumer.broken_instance: registered but unresolvable"),
], ids=["no_registration", "unresolvable"])
def test_a_never_imported_skipped_file_is_checked_at_the_first_injection(make_project, caplog, body, problem):
    _root, tag = make_project(body, extra=OTHER)
    importlib.import_module(f"app_{tag}")
    assert f"main_{tag}" not in sys.modules                 # nobody imports it
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        _first_injection(tag)
    assert f"main_{tag}" in sys.modules
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", problem)) == 1


def test_with_validate_the_first_injection_raises_for_a_never_imported_file(make_project):
    _root, tag = make_project(MISSING, validate=True, extra=OTHER)
    importlib.import_module(f"app_{tag}")
    error = _call(_first_injection, tag)
    assert isinstance(error, AutowiringValidationError)
    assert error.issues == (f"main_{tag}.Consumer.missing_thing_instance: no matching registration",)


def test_a_never_imported_healthy_file_is_loaded_checked_and_filled(make_project, caplog):
    _root, tag = make_project(GOOD, extra=OTHER)
    importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        _first_injection(tag)
        _first_injection(tag)                               # loaded once, not again
    assert len(_records(caplog, logging.INFO, "Checked", f"main_{tag}")) == 1
    assert sys.modules[f"main_{tag}"].Consumer().clock_instance.now() == "tick"
    assert getattr(autowired, "_AWAITING", None) is not None and f"main_{tag}" not in autowired._AWAITING


def test_validate_autowiring_loads_a_never_imported_skipped_file(make_project):
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    error = _call(autowired.validate_autowiring, app.container)
    assert isinstance(error, AutowiringValidationError)
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in error.issues
    assert "Not checked yet" not in str(error)


@pytest.mark.parametrize("call", ["refresh", "activate"])
def test_refresh_and_activate_load_a_never_imported_skipped_file(make_project, caplog, call):
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        if call == "refresh":
            autowired.refresh()
        else:
            autowired.activate_container(app.container)
    assert len(_records(caplog, logging.ERROR, f"main_{tag}", "no matching registration")) == 1


def test_a_skipped_file_is_not_loaded_while_its_blocker_is_still_loading(make_project, caplog):
    """An injection inside the app module, after auto_inject(): main must wait (it would fail again)."""
    _root, tag = make_project(GOOD, extra=OTHER, app_tail="from other_TAG import Other\nOther()")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"app_{tag}")
    assert f"main_{tag}" not in sys.modules
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
    _first_injection(tag)                                   # the app finished: now it loads
    assert f"main_{tag}" in sys.modules


def test_a_real_import_failure_found_later_is_a_warning_once(make_project, caplog):
    _root, tag = make_project("import does_not_exist_pkg_0541\n" + GOOD, extra=OTHER)
    importlib.import_module(f"app_{tag}")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        _first_injection(tag)
        _first_injection(tag)
        # Written by the background writer (this may run inside a resolution).
        deadline = time.monotonic() + 3
        while not _records(caplog, logging.WARNING, f"Import failed for main_{tag}") and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)                                     # room for a wrong second line
    assert len(_records(caplog, logging.WARNING, f"Import failed for main_{tag}", "does_not_exist_pkg_0541")) == 1


# ---- check_skipped(): explicit check for tests --------------------------------------------

def test_check_skipped_raises_for_any_problem_even_without_validate(make_project):
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    check = getattr(py_autowired, "check_skipped", None)
    assert callable(check) and "check_skipped" in py_autowired.__all__
    importlib.import_module(f"main_{tag}")                  # already checked once (ERROR logged)
    error = _call(check, app.container)
    assert isinstance(error, AutowiringValidationError)
    assert error.issues == (f"main_{tag}.Consumer.missing_thing_instance: no matching registration",)


def test_check_skipped_returns_the_checked_names_when_healthy(make_project):
    _root, tag = make_project(GOOD)
    importlib.import_module(f"app_{tag}")
    check = getattr(py_autowired, "check_skipped", lambda *a: None)
    assert check() == (f"main_{tag}",)                      # default: the active container
    assert f"main_{tag}" in sys.modules


def test_check_skipped_reports_a_file_that_cannot_be_imported(make_project):
    _root, tag = make_project("import does_not_exist_pkg_0542\n" + GOOD)
    app = importlib.import_module(f"app_{tag}")
    check = getattr(py_autowired, "check_skipped", lambda *a: None)
    error = _call(check, app.container)
    assert isinstance(error, AutowiringValidationError)
    assert len(error.issues) == 1 and error.issues[0].startswith(f"main_{tag}: could not be imported: ")
    assert "does_not_exist_pkg_0542" in error.issues[0]


def test_check_skipped_reports_a_file_that_is_still_loading(make_project, monkeypatch):
    _root, tag = make_project(GOOD)
    app = importlib.import_module(f"app_{tag}")
    loading = type(sys)(f"main_{tag}")
    loading.__spec__ = importlib.machinery.ModuleSpec(f"main_{tag}", None)
    loading.__spec__._initializing = True
    monkeypatch.setitem(sys.modules, f"main_{tag}", loading)
    check = getattr(py_autowired, "check_skipped", lambda *a: None)
    error = _call(check, app.container)
    assert isinstance(error, AutowiringValidationError)
    assert error.issues == (f"main_{tag}: is still being imported; call check_skipped() after startup",)


def test_check_skipped_without_any_container_says_so(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    check = getattr(py_autowired, "check_skipped", lambda *a: None)
    error = _call(check)
    assert isinstance(error, py_autowired.DependencyInjectionError)
    assert "auto_inject" in str(error)


# ---- branch guards for the load-time machinery ---------------------------------------------

@pytest.mark.parametrize("call", ["refresh", "activate"])
def test_refresh_and_activate_raise_what_their_own_drain_found(make_project, monkeypatch, call):
    _root, tag = make_project(MISSING, validate=True)
    app = importlib.import_module(f"app_{tag}")
    monkeypatch.setattr(autowired, "_drain_when_free", lambda: None, raising=False)   # stays queued
    _import_while_the_lock_is_busy(f"main_{tag}", wait_for_drain=False)
    monkeypatch.setattr(autowired, "_load_skipped", lambda *a, **k: None, raising=False)
    if call == "refresh":
        error = _call(autowired.refresh)
    else:
        error = _call(autowired.activate_container, app.container)
    assert isinstance(error, AutowiringValidationError)


def test_check_skipped_reports_a_file_whose_import_fails_validation(make_project):
    _root, tag = make_project(MISSING, validate=True)
    app = importlib.import_module(f"app_{tag}")
    check = getattr(py_autowired, "check_skipped", lambda *a: None)
    error = _call(check, app.container)                     # never imported before: the import fails
    assert isinstance(error, AutowiringValidationError)
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in error.issues


def test_check_skipped_patches_and_checks_a_file_loaded_around_the_hook(make_project, monkeypatch):
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "meta_path", [f for f in sys.meta_path
                                         if not isinstance(f, autowired._LateImportFinder)])
        importlib.import_module(f"main_{tag}")
    check = getattr(py_autowired, "check_skipped", lambda *a: None)
    error = _call(check, app.container)
    assert isinstance(error, AutowiringValidationError)
    assert error.issues == (f"main_{tag}.Consumer.missing_thing_instance: no matching registration",)
    assert f"main_{tag}" not in getattr(autowired, "_DEFERRED", {})


def test_check_skipped_reports_a_file_it_cannot_check(make_project, monkeypatch):
    _root, tag = make_project(GOOD)
    app = importlib.import_module(f"app_{tag}")
    importlib.import_module(f"main_{tag}")

    def broken(*_args):
        raise RuntimeError("cannot patch")

    monkeypatch.setattr(autowired, "_patch_module_classes", broken)
    check = getattr(py_autowired, "check_skipped", lambda *a: None)
    error = _call(check, app.container)
    assert isinstance(error, AutowiringValidationError)
    assert error.issues == (f"main_{tag}: could not be checked: RuntimeError('cannot patch')",)


def test_validate_autowiring_checks_a_loaded_skipped_file_the_automatic_path_missed(make_project, monkeypatch):
    """The automatic load ran while no container was active and could not check; validation still does."""
    _root, tag = make_project(MISSING)
    app = importlib.import_module(f"app_{tag}")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "meta_path", [f for f in sys.meta_path
                                         if not isinstance(f, autowired._LateImportFinder)])
        importlib.import_module(f"main_{tag}")              # loaded around the hook: not checked
    cache = autowired._NAME_MAP_CACHE
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    autowired._load_skipped()                               # takes it off the waiting list, cannot check
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    error = _call(autowired.validate_autowiring, app.container)
    assert isinstance(error, AutowiringValidationError)
    assert f"main_{tag}.Consumer.missing_thing_instance: no matching registration" in error.issues
