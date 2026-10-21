"""0.5.4: no problem hides behind try/except, a debug-only message or a quiet default.

Each test sets up one case that used to pass silently (or only with
PY_AUTOWIRED_DEBUG=1) and checks that it is now visible: a WARNING, a
validation issue, or the error Python itself would raise. Runtime behaviour
(what gets injected) does not change.
"""

import abc
import importlib
import importlib.abc
import importlib.util
import logging
import sys
import textwrap
import threading
import time
import uuid

import pytest

from py_autowired import AutowiringValidationError, Container, autowired, validate_autowiring


def _warnings(caplog, *parts):
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and all(p in r.getMessage() for p in parts)]


def _call(function, *args):
    try:
        function(*args)
    except Exception as error:  # noqa: BLE001 - the test inspects it
        return error
    return None


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith("hid_"):
            sys.modules.pop(name, None)


def _project(tmp_path, monkeypatch, files):
    tag = uuid.uuid4().hex[:8]
    root = tmp_path / f"hid_{tag}"
    root.mkdir()
    (root / "requirements.txt").write_text("")
    for name, body in files.items():
        path = root / name.replace("TAG", tag)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).replace("TAG", tag))
    monkeypatch.syspath_prepend(str(root))
    return root, tag


# ---- K2: a class that cannot be tracked is never silently left out of validation ------------

def test_an_untracked_class_is_a_warning_and_a_validation_issue(isolated, monkeypatch, caplog):
    container = Container()

    class Consumer:
        missing_instance = None

    def broken(*_args):
        raise RuntimeError("cannot track")

    monkeypatch.setattr(autowired, "_remember_patched_class", broken)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired._patch_module_classes(type(sys)("hid_mod"), container, {})   # nothing in it
        autowired._patch_init(Consumer, container, {})
    assert len(_warnings(caplog, "Consumer", "could not be tracked")) == 1
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError)
    assert any("Consumer: could not be tracked for validation" in issue for issue in error.issues)


def test_an_untracked_class_without_fields_is_visible_too(isolated, monkeypatch, caplog):
    container = Container()
    module = type(sys)("hid_plain")

    class Plain:
        pass

    Plain.__module__ = "hid_plain"
    module.Plain = Plain

    def broken(*_args):
        raise RuntimeError("cannot track")

    monkeypatch.setattr(autowired, "_remember_patched_class", broken)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired._patch_module_classes(module, container, {})
    assert len(_warnings(caplog, "Plain", "could not be tracked")) == 1


# ---- K3: a non-callable __init__ fails as Python would, instead of being swallowed ----------

def test_a_non_callable_init_raises_like_python(isolated):
    class Broken:
        __init__ = None
        clock_instance = None

    autowired._patch_init(Broken, Container(), {})
    error = _call(Broken)
    assert isinstance(error, TypeError)


# ---- K4: a field with no registration is a WARNING at runtime (once per field) ------------

def test_an_unregistered_field_is_a_warning_once(isolated, caplog):
    container = Container()

    class Consumer:
        def __init__(self):
            self.nothing_registered_instance = None

    autowired._patch_setattr(Consumer, container, {})
    autowired._patch_init(Consumer, container, {})
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        Consumer()
        Consumer()
    found = _warnings(caplog, "Consumer.nothing_registered_instance", "no matching registration")
    assert len(found) == 1
    assert Consumer().nothing_registered_instance is None              # behaviour unchanged


# ---- K5: a discovery error outside the import itself is a WARNING ---------------------------

def test_a_discovery_path_error_is_a_warning(tmp_path, monkeypatch, caplog):
    (tmp_path / "hid_some_module.py").write_text("X = 1\n")

    def explode(*_args):
        raise RuntimeError("path trouble")

    monkeypatch.setattr(autowired, "_import_module_safely", explode)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired._discover_and_load_modules(tmp_path)
    assert len(_warnings(caplog, "hid_some_module", "path trouble")) == 1


# ---- K6: a module whose path cannot be read is not skipped silently -------------------------

def test_a_module_with_an_unreadable_path_is_a_warning(isolated, tmp_path, monkeypatch, caplog):
    odd = type(sys)("hid_odd_path")
    odd.__file__ = 12345                                    # not a path
    monkeypatch.setitem(sys.modules, "hid_odd_path", odd)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(Container(), root_dir=str(tmp_path))
    assert len(_warnings(caplog, "hid_odd_path")) == 1


# ---- K7/K8: validation never passes what the container would fail ---------------------------

def _bad_signature_init(self, dependency):
    self.dependency = dependency


_bad_signature_init.__signature__ = "not a signature"


class OpaqueService:
    __init__ = _bad_signature_init


class HintedService:
    def __init__(self, dependency: "NotDefinedAnywhere054"):  # noqa: F821
        self.dependency = dependency


class HintedWithDefault:
    def __init__(self, dependency: "NotDefinedAnywhere054" = None):  # noqa: F821
        self.dependency = dependency


def _graph_with(service):
    container = Container()
    container.register_singleton(service)

    Consumer = type("Consumer", (), {f"{service.__name__.lower()}_instance": None,
                                      "__module__": "hid_consumers"})
    autowired._patch_setattr(Consumer, container, {})
    autowired._patch_init(Consumer, container, {})
    return container


def test_validation_reports_a_constructor_whose_signature_cannot_be_read(isolated):
    container = _graph_with(OpaqueService)
    assert isinstance(_call(container.resolve, OpaqueService), TypeError)       # what runtime does
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError)
    assert any("OpaqueService: constructor signature cannot be read" in issue for issue in error.issues)


def test_validation_reports_a_type_hint_that_cannot_be_resolved(isolated):
    container = _graph_with(HintedService)
    assert _call(container.resolve, HintedService) is not None                  # what runtime does
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError)
    assert any("HintedService.dependency: type hint cannot be resolved" in issue for issue in error.issues)


def test_validation_warns_when_a_default_replaces_an_unresolved_hint(isolated, caplog):
    container = _graph_with(HintedWithDefault)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        assert validate_autowiring(container) is None
    assert len(_warnings(caplog, "HintedWithDefault.dependency", "cannot be resolved", "default")) == 1


# ---- K9: the container says so when type hints cannot be resolved -------------------------

def test_resolving_with_unresolvable_hints_is_a_warning_once(caplog):
    container = Container()

    class LocalHinted:
        def __init__(self, dependency: "AlsoNotDefined054" = None):  # noqa: F821
            self.dependency = dependency

    container.register_transient(LocalHinted)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        first = container.resolve(LocalHinted)
        container.resolve(LocalHinted)
    assert first.dependency is None                                             # behaviour unchanged
    assert len(_warnings(caplog, "LocalHinted", "type hints cannot be resolved")) == 1


# ---- K10: a late import queued while the lock was busy is patched without help ------------

def test_a_queued_late_import_is_patched_without_another_lock_holder(isolated, tmp_path, monkeypatch):
    root, tag = _project(tmp_path, monkeypatch, {
        "hid_clock_TAG.py": "class Clock:\n    pass\n",
        "dist/hid_late_TAG.py": "class Consumer:\n    def __init__(self):\n        self.clock_instance = None\n",
    })
    monkeypatch.syspath_prepend(str(root / "dist"))
    clock = importlib.import_module(f"hid_clock_{tag}")
    container = Container()
    container.register_singleton(clock.Clock)
    autowired.auto_inject(container, root_dir=str(root))
    held, release = threading.Event(), threading.Event()

    def hold():
        with autowired._GLOBAL_LOCK:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    try:
        module = importlib.import_module(f"hid_late_{tag}")                      # queued
    finally:
        release.set()
        holder.join(5)
    deadline = time.monotonic() + 3
    while module.Consumer().clock_instance is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert module.Consumer().clock_instance is not None                         # no refresh() needed


# ---- K11: a root with nothing to inject is a WARNING ------------------------------------

def test_a_root_with_nothing_to_load_is_a_warning(isolated, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(Container(), root_dir=str(tmp_path))
    assert len(_warnings(caplog, "no project module", str(tmp_path.resolve()))) == 1


# ---- K12: a contract with no implementation after discovery is a WARNING ------------------

class IHiddenNothing(abc.ABC):
    @abc.abstractmethod
    def run(self):
        ...


def test_a_contract_without_an_implementation_is_a_warning(isolated, tmp_path, caplog):
    container = Container()
    container.register_singleton(IHiddenNothing)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(container, root_dir=str(tmp_path))
    assert len(_warnings(caplog, "IHiddenNothing", "no implementation")) == 1


# ---- K13: a project module that the import hook cannot patch is a WARNING -----------------

class _OtherLoader(importlib.abc.Loader):
    """Stands in for any loader that is not the plain source loader (pytest's rewriter, .pyc-only, ...)."""

    def __init__(self, path):
        self.path = path

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        exec(compile(open(self.path, encoding="utf-8").read(), self.path, "exec"), module.__dict__)


class _OtherFinder(importlib.abc.MetaPathFinder):
    def __init__(self, files):
        self.files = files

    def find_spec(self, fullname, path, target=None):
        file = self.files.get(fullname)
        if file is None:
            return None
        return importlib.util.spec_from_file_location(fullname, file, loader=_OtherLoader(str(file)))


def test_a_module_loaded_by_another_loader_is_a_warning_once(isolated, tmp_path, monkeypatch, caplog):
    root, tag = _project(tmp_path, monkeypatch, {
        "pkg/hid_other_TAG.py": "class Consumer:\n    clock_instance = None\n",
        "tests/hid_test_TAG.py": "class Consumer:\n    clock_instance = None\n",
    })
    autowired.auto_inject(Container(), root_dir=str(root))
    finder = _OtherFinder({f"hid_other_{tag}": root / "pkg" / f"hid_other_{tag}.py",
                           f"hid_test_{tag}": root / "tests" / f"hid_test_{tag}.py"})
    monkeypatch.setattr(sys, "meta_path", [*sys.meta_path, finder])
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"hid_other_{tag}")
        importlib.import_module(f"hid_test_{tag}")                              # excluded folder: quiet
        sys.modules.pop(f"hid_other_{tag}")
        importlib.import_module(f"hid_other_{tag}")                              # once per module
    assert len(_warnings(caplog, f"hid_other_{tag}", "not patched", "_OtherLoader")) == 1
    assert _warnings(caplog, f"hid_test_{tag}") == []


# ---- K14: equally good candidates are listed in ONE startup WARNING ------------------------

def test_equally_good_candidates_are_one_grouped_warning(isolated, tmp_path, monkeypatch, caplog):
    """One class registered under two contracts: a field named after the class has two equal candidates."""
    root, tag = _project(tmp_path, monkeypatch, {
        "hid_services_TAG.py": """
            import abc
            class IReader(abc.ABC):
                @abc.abstractmethod
                def get(self): ...
            class IWriter(abc.ABC):
                @abc.abstractmethod
                def put(self): ...
            class SqlStore(IReader, IWriter):
                def get(self): return "sql"
                def put(self): return "sql"
            class IClockA(abc.ABC):
                @abc.abstractmethod
                def now(self): ...
            class IClockB(abc.ABC):
                @abc.abstractmethod
                def now(self): ...
            class SystemClock(IClockA, IClockB):
                def now(self): return 1
        """,
        "hid_users_TAG.py": """
            class First:
                def __init__(self):
                    self.sql_store_instance = None
            class Second:
                def __init__(self):
                    self.system_clock_instance = None
            class Clear:
                def __init__(self):
                    self.reader_instance = None
        """,
    })
    services = importlib.import_module(f"hid_services_{tag}")
    container = Container()
    container.register_singleton(services.IReader, services.SqlStore)
    container.register_singleton(services.IWriter, services.SqlStore)
    container.register_singleton(services.IClockA, services.SystemClock)
    container.register_singleton(services.IClockB, services.SystemClock)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(container, root_dir=str(root))
    found = _warnings(caplog, "equally good candidates")
    assert len(found) == 1
    assert found[0].startswith("2 fields have equally good candidates; the first registered one is used:")
    assert f"hid_users_{tag}.First.sql_store_instance" in found[0]
    assert f"hid_users_{tag}.Second.system_clock_instance" in found[0]
    assert "Clear.reader_instance" not in found[0]
    users = sys.modules[f"hid_users_{tag}"]
    assert users.First().sql_store_instance.get() == "sql"               # behaviour unchanged


# ---- branch guards (each one keeps a mutant from surviving) ---------------------------------

def test_a_collected_containers_untracked_entries_go_with_it(monkeypatch):
    """id() values are reused: a dead container's 'could not be tracked' issues must not
    become a NEW container's validation errors."""
    import gc

    def broken(*_args):
        raise RuntimeError("cannot track")

    monkeypatch.setattr(autowired, "_remember_patched_class", broken)
    container = Container()
    autowired._track(container, type("Lost", (), {}))
    key = id(container)
    assert key in autowired._UNTRACKED
    newer = ["newer container's own entry"]
    del container
    gc.collect()
    assert key not in autowired._UNTRACKED
    with autowired._GLOBAL_LOCK:                      # a late finalizer must spare a newer list
        autowired._UNTRACKED[key] = newer
    autowired._forget_untracked(key, ["the dead container's list"])
    try:
        assert autowired._UNTRACKED.get(key) is newer
    finally:
        autowired._UNTRACKED.pop(key, None)


def test_validation_finishes_finalizers_that_found_the_lock_busy():
    key, classes = 987654322, __import__("weakref").WeakValueDictionary()
    with autowired._GLOBAL_LOCK:
        autowired._PATCHED_CLASSES[key] = classes
        autowired._FORGOTTEN.append((autowired._PATCHED_CLASSES, key, classes))
    validate_autowiring(Container())
    assert key not in autowired._PATCHED_CLASSES


class OpaqueFieldClass:
    __init__ = _bad_signature_init
    clock_instance = None


def test_an_unreadable_signature_of_a_field_injection_class_is_a_warning_not_an_issue(isolated, caplog):
    container = Container()
    container.register_singleton(OpaqueFieldClass)
    OpaqueFieldClass.__di_attrs__ = ("clock_instance",)
    try:
        Consumer = type("Consumer", (), {"opaquefieldclass_instance": None, "__module__": "hid_consumers"})
        autowired._patch_init(Consumer, container, {})
        with caplog.at_level(logging.DEBUG, logger="py_autowired"):
            error = _call(validate_autowiring, container)
        assert error is None                                  # unknown here, so not an issue ...
        assert len(_warnings(caplog, "OpaqueFieldClass", "constructor signature cannot be read")) == 1
    finally:
        del OpaqueFieldClass.__di_attrs__


def test_supplied_instances_are_never_questioned_about_their_constructor(isolated):
    container = Container()
    container.register_instance(OpaqueService, object.__new__(OpaqueService))
    container.register_instance(HintedService, object.__new__(HintedService))
    Consumer = type("Consumer", (), {"opaqueservice_instance": None, "hintedservice_instance": None,
                                     "__module__": "hid_consumers"})
    autowired._patch_init(Consumer, container, {})
    assert validate_autowiring(container) is None


class KnownService:
    pass


class MixedHints:
    def __init__(self, known: KnownService, unknown: "NotDefinedAnywhere054b"):  # noqa: F821
        self.known, self.unknown = known, unknown


def test_only_the_unresolvable_hint_is_reported(isolated):
    container = Container()
    container.register_singleton(KnownService)
    container.register_singleton(MixedHints)
    Consumer = type("Consumer", (), {"mixedhints_instance": None, "__module__": "hid_consumers"})
    autowired._patch_init(Consumer, container, {})
    error = _call(validate_autowiring, container)
    assert isinstance(error, AutowiringValidationError)
    reported = [issue for issue in error.issues if "type hint cannot be resolved" in issue]
    assert len(reported) == 1 and "MixedHints.unknown" in reported[0]


def test_a_root_with_modules_but_no_injectable_class_is_not_a_warning(isolated, tmp_path, caplog):
    (tmp_path / f"hid_plain_{uuid.uuid4().hex[:8]}.py").write_text("class Plain:\n    pass\n")
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(Container(), root_dir=str(tmp_path))
    assert _warnings(caplog, "Nothing to inject") == []


def test_contracts_with_a_factory_or_an_instance_are_not_missing(isolated, tmp_path, caplog):
    class IHasFactory(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    class IHasInstance(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    class Given(IHasInstance):
        def run(self): return 1

    container = Container()
    container.register_factory(IHasFactory, lambda c: None)
    container.register_instance(IHasInstance, Given())
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(container, root_dir=str(tmp_path))
    assert _warnings(caplog, "no implementation") == []


def test_a_non_python_file_from_another_loader_is_not_a_warning(isolated, tmp_path, monkeypatch, caplog):
    root, tag = _project(tmp_path, monkeypatch, {"pkg/hid_data_TAG.txt": "X = 1\n"})
    autowired.auto_inject(Container(), root_dir=str(root))
    finder = _OtherFinder({f"hid_data_{tag}": root / "pkg" / f"hid_data_{tag}.txt"})
    monkeypatch.setattr(sys, "meta_path", [*sys.meta_path, finder])
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        importlib.import_module(f"hid_data_{tag}")
    assert _warnings(caplog, f"hid_data_{tag}") == []


def test_a_duck_typed_container_with_a_factory_or_an_instance_is_not_missing(caplog):
    from types import SimpleNamespace

    class IDuck(abc.ABC):
        @abc.abstractmethod
        def run(self): ...

    def registration(**fields):
        values = {"service_type": IDuck, "implementation_type": None, "factory": None, "has_instance": False}
        values.update(fields)
        return SimpleNamespace(**values)

    duck = SimpleNamespace(registrations={IDuck: registration(factory=lambda c: None)},
                           _keyed={("k", 1): registration(has_instance=True)})
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired._warn_missing_implementations(duck)
    assert _warnings(caplog, "no implementation") == []
    duck.registrations[IDuck] = registration()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired._warn_missing_implementations(duck)
    assert len(_warnings(caplog, "IDuck", "no implementation")) == 1


def test_candidates_with_different_scores_are_not_listed_as_equal(isolated, tmp_path, monkeypatch, caplog):
    """Two candidates, one in the field's own module: a clear winner, so no grouped WARNING."""
    root, tag = _project(tmp_path, monkeypatch, {
        "hid_far_TAG.py": """
            import abc
            class IStore(abc.ABC):
                @abc.abstractmethod
                def get(self): ...
            class FarStore(IStore):
                def get(self): return "far"
        """,
        "hid_near_TAG.py": """
            import abc
            class ABS_Store(abc.ABC):
                @abc.abstractmethod
                def get(self): ...
            class NearStore(ABS_Store):
                def get(self): return "near"
            class User:
                def __init__(self):
                    self.store_instance = None
        """,
    })
    far = importlib.import_module(f"hid_far_{tag}")
    near = importlib.import_module(f"hid_near_{tag}")
    container = Container()
    container.register_singleton(far.IStore, far.FarStore)
    container.register_singleton(near.ABS_Store, near.NearStore)
    candidates = autowired._field_candidates(autowired._build_name_map_from_container(container), "store_instance")
    assert len(candidates) == 2                                    # two candidates, different scores
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.auto_inject(container, root_dir=str(root))
    assert _warnings(caplog, "equally good candidates") == []


def test_a_new_container_never_writes_into_a_dead_containers_untracked_list(monkeypatch):
    """A dead container's list still waiting for its finalizer must not swallow the new one's issue."""
    def broken(*_args):
        raise RuntimeError("cannot track")

    monkeypatch.setattr(autowired, "_remember_patched_class", broken)
    container = Container()
    key, dead = id(container), ["dead container's entry"]
    with autowired._GLOBAL_LOCK:
        autowired._UNTRACKED[key] = dead
        autowired._FORGOTTEN.append((autowired._UNTRACKED, key, dead))   # its finalizer found the lock busy
    try:
        autowired._track(container, type("Fresh", (), {}))
        error = _call(validate_autowiring, container)
        assert isinstance(error, AutowiringValidationError)
        assert any("Fresh: could not be tracked" in issue for issue in error.issues)
        assert not any("dead container" in issue for issue in error.issues)
    finally:
        autowired._UNTRACKED.pop(key, None)
