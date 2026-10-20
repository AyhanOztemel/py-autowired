import logging
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import warnings

import pytest

from py_autowired import Container, autowired


@pytest.mark.parametrize("error", [ImportError("missing module"), RuntimeError("broken module")])
def test_discovery_failures_always_log_without_python_warnings(monkeypatch, caplog, capsys, error):
    monkeypatch.setattr(autowired, "DEBUG_MODE", False)

    def fail(name):
        raise error

    monkeypatch.setattr(autowired.importlib, "import_module", fail)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with caplog.at_level(logging.WARNING, logger="py_autowired"):
            assert autowired._import_module_safely("broken_module", "unused.py") is False
    assert "Import failed for broken_module" in caplog.text
    assert all(record.name == "py_autowired" for record in caplog.records)
    assert caplog.records[0].levelno == logging.WARNING
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("debug", [False, True])
def test_logger_handlers_and_console_behavior_in_fresh_process(debug, tmp_path):
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src")
    environment["PY_AUTOWIRED_DEBUG"] = "true" if debug else "false"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.pop("DI_DEBUG", None)
    script = '''
        from contextlib import redirect_stdout
        from io import StringIO
        import logging
        import sys
        import warnings
        from py_autowired import Container, auto_inject, print_cache_stats
        logger = logging.getLogger("py_autowired")
        own = [h for h in logger.handlers if getattr(h, "_py_autowired_handler", False)]
        assert len(own) == 1
        enabled = sys.argv[2] == "true"
        assert isinstance(own[0], logging.StreamHandler if enabled else logging.NullHandler)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            runtime_stdout = StringIO()
            with redirect_stdout(runtime_stdout):
                auto_inject(Container(), root_dir=sys.argv[1])
            assert runtime_stdout.getvalue() == ""
            print_cache_stats()
    '''
    result = subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(script), str(tmp_path), str(debug).lower()],
        cwd=root, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Cache Statistics:" in result.stdout
    assert "Resolution cache entries:" in result.stdout
    assert "Cache Statistics:" not in result.stderr
    if debug:
        assert "Starting automatic injection" in result.stderr
    else:
        assert result.stderr == ""


def test_debug_disabled_runtime_leaves_stdout_empty(monkeypatch, capsys):
    monkeypatch.setattr(autowired, "DEBUG_MODE", False)
    container = Container()

    class Service:
        pass

    class Consumer:
        service_instance = None
        unknown_instance = None

    container.register_transient(Service)
    cache = autowired._CachedNameMap(container)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", cache)
    name_map = cache.get_raw_map()
    autowired._patch_setattr(Consumer, container, name_map)
    autowired._patch_init(Consumer, container, name_map)
    assert isinstance(Consumer().service_instance, Service)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("debug", [False, True])
def test_explicit_print_cache_stats_writes_stdout_without_logging(monkeypatch, capsys, caplog, debug):
    monkeypatch.setattr(autowired, "DEBUG_MODE", debug)
    stats = autowired.get_cache_stats()
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        autowired.print_cache_stats()
    captured = capsys.readouterr()
    assert captured.out == (
        "\n📊 Cache Statistics:\n"
        f"  _norm: hits={stats['norm_cache']['hits']}, misses={stats['norm_cache']['misses']}\n"
        f"  _calculate_module_similarity: hits={stats['similarity_cache']['hits']}, misses={stats['similarity_cache']['misses']}\n"
        f"  Resolution cache entries: {stats['resolution_cache_size']}\n"
    )
    assert captured.err == ""
    assert caplog.records == []
