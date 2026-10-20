"""Fresh-process checks avoid importing the optional bridge before comparison."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_probe(source):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(source)],
        cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_core_imports_and_autowiring_work_without_fastapi():
    run_probe('''
        import importlib.abc
        import sys

        class NoFastAPI(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "fastapi" or fullname.startswith("fastapi."):
                    raise ImportError("FastAPI deliberately unavailable")

        sys.meta_path.insert(0, NoFastAPI())
        import py_autowired
        import test_autowired as existing
        existing.test_nested_runtime_injection_uses_instance_fields_only()
        existing.test_lifetimes_are_preserved()
        existing.test_non_none_manual_assignment_is_not_overwritten()
        existing.test_async_scope_works()
        assert not any(name == "fastapi" or name.startswith("fastapi.") for name in sys.modules)
        assert "py_autowired.fastapi" not in sys.modules
    ''')


def test_existing_scenarios_are_identical_before_and_after_bridge_import():
    pytest.importorskip("fastapi")
    run_probe('''
        import test_autowired as existing
        from py_autowired import Container, autowired

        def snapshot():
            existing.test_nested_runtime_injection_uses_instance_fields_only()
            existing.test_lifetimes_are_preserved()
            existing.test_non_none_manual_assignment_is_not_overwritten()
            existing.test_async_scope_works()
            with existing.container.create_scope():
                first, second = existing.Controller(), existing.Controller()
                return (
                    first.service_instance.execute(),
                    type(first.service_instance).__name__,
                    type(first.service_instance.repository_instance).__name__,
                    first.service_instance is not second.service_instance,
                    first.service_instance.repository_instance is second.service_instance.repository_instance,
                )

        def field_semantics():
            container = Container()
            order = []
            class First: pass
            class Second: pass
            def first_factory(owner):
                assert owner is container
                order.append("first")
                return First()
            def second_factory(owner):
                assert owner is container
                order.append("second")
                return Second()
            container.register_factory(First, first_factory)
            container.register_factory(Second, second_factory)
            marker = object()
            class Eager:
                def __init__(self):
                    self.second_instance = None
                    self.first_instance = None
                    self.unregistered_instance = None
            class Fields:
                second_instance = None
                first_instance = None
                unregistered_instance = None
            class Manual:
                def __init__(self):
                    self.first_instance = marker
            class Aliased:
                __di_compat_aliases__ = {"old_first": "first_instance"}
                def __init__(self):
                    self.old_first = None
            previous = autowired._NAME_MAP_CACHE
            try:
                autowired.activate_container(container)
                name_map = autowired._NAME_MAP_CACHE.get_raw_map()
                for cls in (Eager, Fields, Manual, Aliased):
                    autowired._patch_setattr(cls, container, name_map)
                    autowired._patch_init(cls, container, name_map)
                eager = Eager()
                assert order == ["second", "first"]
                assert eager.unregistered_instance is None
                order.clear()
                fields = Fields()
                assert order == ["first", "second"]
                assert fields.unregistered_instance is None
                order.clear()
                assert Manual().first_instance is marker
                assert order == []
                aliased = Aliased()
                assert aliased.old_first is aliased.first_instance
                return (type(eager.first_instance).__name__, type(fields.second_instance).__name__,
                        tuple(order), eager.unregistered_instance, fields.unregistered_instance)
            finally:
                autowired._NAME_MAP_CACHE = previous

        before, before_fields = snapshot(), field_semantics()
        active_cache = autowired._NAME_MAP_CACHE
        import py_autowired.fastapi
        assert autowired._NAME_MAP_CACHE is active_cache
        assert snapshot() == before == ("now", "Service", "Repository", True, True)
        assert field_semantics() == before_fields == ("First", "Second", ("first",), None, None)
    ''')
