from __future__ import annotations

from abc import ABC, abstractmethod

from py_autowired import autowired
from py_autowired.container import Container


class ABS_PrimaryClock(ABC):
    @abstractmethod
    def now(self) -> str:
        raise NotImplementedError


class ABS_AliasClock(ABC):
    @abstractmethod
    def now(self) -> str:
        raise NotImplementedError


class ConcreteClock(ABS_PrimaryClock):
    def now(self) -> str:
        return "now"


class AliasConsumer:
    alias_clock_instance = None


def test_alias_name_injection_reuses_the_exact_target_singleton(monkeypatch) -> None:
    container = Container()
    container.register_singleton(ABS_PrimaryClock, ConcreteClock)
    container.register_alias(ABS_AliasClock, ABS_PrimaryClock)

    target = container.resolve(ABS_PrimaryClock)
    assert container.resolve(ABS_AliasClock) is target

    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    name_map = cache.get_raw_map()
    autowired._patch_setattr(AliasConsumer, container, name_map)
    autowired._patch_init(AliasConsumer, container, name_map)

    assert AliasConsumer().alias_clock_instance is target


def test_auto_inject_exclude_dirs_skips_the_requested_directory(
    monkeypatch,
    tmp_path,
) -> None:
    (tmp_path / "safe_module.py").write_text("VALUE = 1\n", encoding="utf-8")
    excluded = tmp_path / "side_effects"
    excluded.mkdir()
    (excluded / "danger.py").write_text(
        "raise RuntimeError('must not be imported')\n",
        encoding="utf-8",
    )

    imported: list[str] = []

    def record_import(dotted_name: str, _file_path: str) -> bool:
        imported.append(dotted_name)
        return True

    original_defaults = frozenset(autowired._EXCLUDED_DIRS)
    monkeypatch.setattr(autowired, "_import_module_safely", record_import)

    autowired.auto_inject(
        Container(),
        root_dir=str(tmp_path),
        exclude_dirs={"side_effects"},
    )

    assert "safe_module" in imported
    assert "side_effects.danger" not in imported
    assert frozenset(autowired._EXCLUDED_DIRS) == original_defaults


def test_default_excluded_dirs_remain_backward_compatible() -> None:
    assert autowired._EXCLUDED_DIRS == {
        ".venv",
        "venv",
        "env",
        ".env",
        "site-packages",
        "__pycache__",
        ".git",
        "node_modules",
        "build",
        "dist",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "egg-info",
        "tests",
        "examples",
    }
