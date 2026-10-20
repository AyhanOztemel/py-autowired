"""Tests for behavior that mutation testing showed was unprotected:
project-root search, module discovery rules, registration policies,
constructor options, cycle reporting and unresolvable annotations."""

import sys
import types
import uuid

import pytest

from py_autowired import (
    CircularDependencyError, Container, DuplicateServiceRegistrationError,
    MissingServiceReplacementError, autowired,
)


# ---- project root search -------------------------------------------------
def _fake_main(monkeypatch, file_path):
    main = types.ModuleType("__main__")
    main.__file__ = str(file_path)
    monkeypatch.setitem(sys.modules, "__main__", main)


def test_root_is_nearest_ancestor_with_a_marker(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")
    deep = tmp_path / "src" / "app"
    deep.mkdir(parents=True)
    _fake_main(monkeypatch, deep / "run.py")
    assert autowired._find_project_root() == tmp_path.resolve()


@pytest.mark.parametrize("marker", ["requirements.txt", "setup.py", "Pipfile", ".git", "main.py", "app.py"])
def test_every_documented_marker_is_recognised(tmp_path, monkeypatch, marker):
    target = tmp_path / marker
    target.mkdir() if marker == ".git" else target.write_text("")
    deep = tmp_path / "pkg"
    deep.mkdir()
    _fake_main(monkeypatch, deep / "x.py")
    assert autowired._find_project_root() == tmp_path.resolve()


def test_marker_in_start_directory_wins(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")
    start = tmp_path / "svc"
    start.mkdir()
    (start / "setup.py").write_text("")
    _fake_main(monkeypatch, start / "run.py")
    assert autowired._find_project_root() == start.resolve()


def test_without_main_file_the_working_directory_is_used(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")
    work = tmp_path / "w"
    work.mkdir()
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    monkeypatch.chdir(work)
    assert autowired._find_project_root() == tmp_path.resolve()


# ---- module discovery ------------------------------------------------------
def test_discovery_imports_modules_and_skips_excluded_and_private(tmp_path, monkeypatch):
    pkg = f"disc_{uuid.uuid4().hex[:8]}"
    root = tmp_path
    (root / pkg).mkdir()
    (root / pkg / "__init__.py").write_text("")
    (root / pkg / "good.py").write_text("VALUE = 1\n")
    (root / pkg / "_private.py").write_text("raise RuntimeError('must not import')\n")
    for excluded in ("venv", "tests", "custom_skip"):
        (root / pkg / excluded).mkdir()
        (root / pkg / excluded / "__init__.py").write_text("")
        (root / pkg / excluded / "bad.py").write_text("raise RuntimeError('excluded')\n")
    monkeypatch.syspath_prepend(str(root))
    loaded = autowired._discover_and_load_modules(root, exclude_dirs=["custom_skip"])
    assert loaded == 1
    assert f"{pkg}.good" in sys.modules
    assert f"{pkg}._private" not in sys.modules


def test_exclude_dirs_must_not_be_a_string():
    with pytest.raises(TypeError):
        autowired._normalize_excluded_dirs("venv")
    assert "custom" in autowired._normalize_excluded_dirs(["custom"])
    assert "venv" in autowired._normalize_excluded_dirs(None)


# ---- container options and registration policies ---------------------------
@pytest.mark.parametrize("kwargs", [{"auto_discover": True}, {"discovery_root": "."}])
def test_filesystem_discovery_is_not_part_of_the_core_container(kwargs):
    with pytest.raises(ValueError, match="auto_inject"):
        Container(**kwargs)


class Service:
    pass


class Other(Service):
    pass


def test_registration_policies():
    container = Container()
    container.register_transient(Service)
    assert container.register_fallback(Service, Other) is False      # keeps the original
    assert type(container.resolve(Service)) is Service
    container.replace(Service, Other)
    assert type(container.resolve(Service)) is Other
    with pytest.raises(MissingServiceReplacementError):
        container.replace(Other, Other)
    strict = Container(strict_registrations=True)
    strict.register_transient(Service)
    with pytest.raises(DuplicateServiceRegistrationError):
        strict.register_transient(Service)
    loose = Container()
    loose.register_transient(Service)
    loose.register(Service, Other)                                     # last wins
    assert type(loose.resolve(Service)) is Other
    assert [p for p, _ in container.registration_history] == ["register", "fallback-skipped", "replace"]


# ---- cycles and annotations ------------------------------------------------
class CycleA:
    def __init__(self, b: "CycleB"):
        self.b = b


class CycleB:
    def __init__(self, a: CycleA):
        self.a = a


def test_cycle_error_names_the_full_path():
    container = Container()
    container.register_transient(CycleA)
    container.register_transient(CycleB)
    with pytest.raises(CircularDependencyError, match=r"Circular dependency: CycleA -> CycleB -> CycleA"):
        container.resolve(CycleA)


def test_unresolvable_annotation_falls_back_to_defaults():
    class UsesUnknown:
        def __init__(self, thing: "DoesNotExist" = 5):
            self.thing = thing

    container = Container()
    container.register_transient(UsesUnknown)
    assert container.resolve(UsesUnknown).thing == 5
