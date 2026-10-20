"""Debug output contract: silent unless debug mode is on; informative when on."""

import logging
import sys
import types
import uuid

from py_autowired import Container, autowired


def _run_autowiring(tmp_path, monkeypatch):
    name = f"dbg_{uuid.uuid4().hex[:8]}"
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "services.py").write_text("class Clock:\n    pass\n")
    (pkg / "consumers.py").write_text(
        f"from {name}.services import Clock\n"
        "class Consumer:\n"
        "    clock_instance = None\n"
        "class Plain:\n"
        "    pass\n"
    )
    (pkg / "_skipped.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(sys.modules, "__main__", types.ModuleType("__main__"))
    services = __import__(f"{name}.services", fromlist=["Clock"])
    container = Container()
    container.register_transient(services.Clock)
    autowired.auto_inject(container, root_dir=str(tmp_path))
    consumers = sys.modules[f"{name}.consumers"]
    consumer = consumers.Consumer()
    consumer.clock_instance = None            # eager path through __setattr__
    autowired.refresh()
    return consumer, services.Clock


def test_no_debug_records_when_debug_mode_is_off(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(autowired, "DEBUG_MODE", False)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        consumer, clock = _run_autowiring(tmp_path, monkeypatch)
    assert isinstance(consumer.clock_instance, clock)
    assert [r for r in caplog.records if r.levelno == logging.DEBUG] == []


def test_debug_records_describe_the_wiring_when_on(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(autowired, "DEBUG_MODE", True)
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", None)
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        consumer, clock = _run_autowiring(tmp_path, monkeypatch)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert isinstance(consumer.clock_instance, clock)
    for expected in (
        "Starting automatic injection",
        "Name map built:",
        "'clock' -> ['Clock']",
        "Selected: Clock",
        "Consumer.clock_instance <- Clock (post-init injection)",
        "Consumer.clock_instance <- Clock (eager injection)",
        "Patched Consumer",
        "Skipped Plain (no injection candidates)",
        "Module loaded",
        "Name map refreshed; resolution cache cleared",
        "Autowiring complete",
    ):
        assert expected in text, expected
    assert "_skipped" not in text.split("Module loading summary:")[0].split("Loading:")[-1]
