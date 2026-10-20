"""Mutation-driven tests for module discovery and cache utilities."""

import sys
import types
import uuid
from pathlib import Path

import pytest

from py_autowired import Container, autowired


def _pkg(tmp_path, monkeypatch):
    name = f"disc_{uuid.uuid4().hex[:8]}"
    (tmp_path / name).mkdir()
    (tmp_path / name / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    return name, tmp_path / name


def test_discovery_skip_rules(tmp_path, monkeypatch):
    name, pkg = _pkg(tmp_path, monkeypatch)
    # Importable on purpose: a broken skip rule must show up as an import,
    # not be hidden by discovery swallowing an import error.
    plain = "X = 1\n"
    (pkg / "good.py").write_text(plain)
    (pkg / "auto_injector_tool.py").write_text(plain)
    (pkg / "my_py_autowired_ext.py").write_text(plain)
    (pkg / "venv").mkdir()
    (pkg / "venv" / "__init__.py").write_text("")
    (pkg / "venv" / "bad.py").write_text(plain)
    (pkg / "zz").mkdir()
    (pkg / "zz" / "__init__.py").write_text("")
    (pkg / "zz" / "late.py").write_text("Y = 2\n")             # after an excluded directory
    main_file = pkg / "entry.py"
    main_file.write_text(plain)
    main = types.ModuleType("__main__")
    main.__file__ = str(main_file)
    monkeypatch.setitem(sys.modules, "__main__", main)
    loaded = autowired._discover_and_load_modules(tmp_path)
    assert loaded == 2
    assert f"{name}.good" in sys.modules and f"{name}.zz.late" in sys.modules
    for skipped in ("auto_injector_tool", "my_py_autowired_ext", "entry", "venv.bad"):
        assert f"{name}.{skipped}" not in sys.modules


def test_root_search_stops_at_home(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("")          # a marker ABOVE home
    home = tmp_path / "home"
    start = home / "proj" / "app"
    start.mkdir(parents=True)
    monkeypatch.setattr(autowired.Path, "home", classmethod(lambda cls: home))
    main = types.ModuleType("__main__")
    main.__file__ = str(start / "run.py")
    monkeypatch.setitem(sys.modules, "__main__", main)
    assert autowired._find_project_root() == start.resolve()


@pytest.mark.parametrize("other", ["a_first", "z_last"])
def test_origin_mismatch_detected_in_both_path_orders(tmp_path, monkeypatch, caplog, other):
    name = f"orig_{uuid.uuid4().hex[:8]}"
    real = tmp_path / other
    real.mkdir()
    (real / f"{name}.py").write_text("")
    monkeypatch.syspath_prepend(str(real))
    expected = tmp_path / "m_middle" / f"{name}.py"
    assert autowired._import_module_safely(name, str(expected)) is False
    sys.modules.pop(name, None)


def test_activate_container_with_and_without_existing_cache(monkeypatch):
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    first = Container()
    autowired.activate_container(first)
    assert autowired._NAME_MAP_CACHE._container is first
    cache = autowired._NAME_MAP_CACHE
    autowired.activate_container(first)
    assert autowired._NAME_MAP_CACHE is cache                 # same container: cache kept
    second = Container()
    autowired.activate_container(second)
    assert autowired._NAME_MAP_CACHE._container is second


def test_cache_stats_report_resolution_entries(monkeypatch):
    container = Container()
    container.register_transient(type("Clock", (), {"__module__": "x.clock"}))
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    cache.get_cached_registration("clock", "x.consumer")
    stats = autowired.get_cache_stats()
    assert stats["resolution_cache_size"] == 1
    assert set(stats) == {"norm_cache", "similarity_cache", "resolution_cache_size"}
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    assert autowired.get_cache_stats()["resolution_cache_size"] == 0
