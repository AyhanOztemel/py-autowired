"""The thread-birth hook (free-threaded 3.14) next to a real coverage.py run.

coverage.py measures with sys.monitoring (its "sysmon" core, tool id COVERAGE_ID = 1) or with a C
tracer ("ctrace"); the library's hook takes tool id 4 (or 3). Under `coverage run`, in a separate
process: the hook is set up ("active"), its id and coverage's never clash, the matrix rows keep their
answers (a thread born inside a scope does not see it; context handed over explicitly does), and
coverage still measures the lines a child thread runs. If an id were taken, the library would fall
back to the hard rule with one WARNING - that is checked too (the result is reported either way).
Skipped where coverage is not installed or threads do not inherit their context."""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import py_autowired

coverage = pytest.importorskip("coverage", reason="coverage.py is not installed in this environment")
pytestmark = pytest.mark.skipif(not getattr(sys.flags, "thread_inherit_context", 0),
                                reason="threads inherit their context only on the free-threaded build")

SCRIPT = textwrap.dedent('''
    import contextvars, logging, sys, threading
    logging.basicConfig(level=logging.WARNING)
    import py_autowired
    from py_autowired import Container, ScopeNotActiveError

    ours = [i for i in range(6) if sys.monitoring.get_tool(i) == "py_autowired thread birth"]
    print("STATUS", py_autowired._thread_birth.STATUS)
    print("OURS", ours, "COVERAGE_ID_TOOL", repr(sys.monitoring.get_tool(sys.monitoring.COVERAGE_ID)))

    class Tag:
        pass

    c = Container()
    c.register_scoped(Tag)
    out = {}

    def born_inside():  # a thread born inside the scope: it must not see the scope
        try:
            c.resolve(Tag)
            out["born"] = "A"
        except ScopeNotActiveError:
            out["born"] = "HATA"
        out["child_ran"] = True  # CHILD MARKER: coverage must measure this line

    def handed():  # context handed over explicitly: it sees the scope
        try:
            c.resolve(Tag)
            out["handed"] = "A"
        except ScopeNotActiveError:
            out["handed"] = "HATA"

    with c.create_scope():
        c.resolve(Tag)
        t = threading.Thread(target=born_inside)
        t.start()
        t.join()
        ctx = contextvars.copy_context()
        t2 = threading.Thread(target=ctx.run, args=(handed,))
        t2.start()
        t2.join()
    print("BORN", out["born"], "HANDED", out["handed"])
''')


CLASH = (  # both ids the library may take are held by another tool before the import
    "import sys\n"
    "sys.monitoring.use_tool_id(4, 'another tool')\n"
    "sys.monitoring.use_tool_id(3, 'another tool')\n")


@pytest.mark.parametrize("core", ["sysmon", "ctrace"])
@pytest.mark.parametrize("clash", [False, True])
def test_the_thread_birth_hook_and_coverage_work_together(core, clash, tmp_path):
    script = tmp_path / "under_coverage.py"
    script.write_text((CLASH if clash else "") + SCRIPT, encoding="utf-8")
    data = tmp_path / ".coverage"
    src = str(Path(py_autowired.__file__).parent.parent)
    env = {**os.environ, "PYTHONPATH": src, "COVERAGE_CORE": core}
    run = subprocess.run([sys.executable, "-m", "coverage", "run", f"--data-file={data}", str(script)],
                         capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    print("--- coverage run, core", core, "exit", run.returncode)
    print(run.stdout)
    print(run.stderr[-3000:])
    assert run.returncode == 0, run.stderr
    report = tmp_path / "cov.json"
    js = subprocess.run([sys.executable, "-m", "coverage", "json", f"--data-file={data}", "-o", str(report)],
                        capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)
    assert js.returncode == 0, js.stderr
    measured = json.loads(report.read_text(encoding="utf-8"))["files"]
    executed = next(v for k, v in measured.items() if k.endswith("under_coverage.py"))["executed_lines"]
    marker = next(n for n, line in enumerate(((CLASH if clash else "") + SCRIPT).splitlines(), 1)
                  if "CHILD MARKER" in line)
    print("child marker line", marker, "measured", marker in executed)

    assert ("STATUS hard-rule" in run.stdout) is clash, run.stdout  # the clash is the only way to fall back
    if "STATUS active" in run.stdout:
        # The hook is set up: its id is not coverage's, and the matrix rows hold.
        assert "OURS [4]" in run.stdout or "OURS [3]" in run.stdout, run.stdout
        assert "'py_autowired thread birth'" not in run.stdout.split("COVERAGE_ID_TOOL", 1)[1], run.stdout
        assert "BORN HATA HANDED A" in run.stdout, run.stdout
        assert "could not set up its thread-birth hook" not in run.stderr, run.stderr
    else:
        # A clash: the hard rule and exactly one WARNING - never silence.
        assert "STATUS hard-rule" in run.stdout, run.stdout
        assert "BORN HATA HANDED HATA" in run.stdout, run.stdout
        assert run.stderr.count("could not set up its thread-birth hook") == 1, run.stderr
    assert marker in executed, f"coverage did not measure the child thread's line ({core})"
