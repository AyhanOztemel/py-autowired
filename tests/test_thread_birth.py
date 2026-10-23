"""0.5.4: on the free-threaded 3.14 build a new thread inherits a copy of its starter's context
(sys.flags.thread_inherit_context). A pool worker born inside one request kept that request's scope
for its whole life and served OTHER requests from it, silently (another request's session). At a
thread's birth the library's own context values (names starting with "py_autowired") are now left
out of the inherited copy, so a 3.14t thread sees what a 3.11 thread sees; context passed on
explicitly (copy_context().run, asyncio.to_thread, anyio, Thread(context=...)) is unchanged.

"own"/"HATA" below: a scoped Tag carries the name of the request whose scope created it; reading it
outside every scope raises ScopeNotActiveError ("HATA"). The matrix rows (M1-M13, L1-L13) come from
the 0.5.4 review; each row lists the results that are correct."""

import asyncio
import concurrent.futures as cf
import concurrent.futures.thread as cft
import contextlib
import contextvars
import functools
import itertools
import logging
import queue
import sys
import threading

import pytest

from py_autowired import Container, _thread_birth
from py_autowired import container as container_module
from py_autowired.container import ScopeNotActiveError

anyio = pytest.importorskip("anyio")
import anyio.to_thread  # noqa: E402

NEW = sys.version_info >= (3, 14)
INHERITS = bool(getattr(sys.flags, "thread_inherit_context", 0))


class Tag:
    name = None


container = Container()
container.register_scoped(Tag)


@contextlib.contextmanager
def scope(name):
    with container.create_scope():
        container.resolve(Tag).name = name
        yield


def probe():
    try:
        return container.resolve(Tag).name
    except ScopeNotActiveError:
        return "HATA"


def bind(fn):  # an explicit hand-over: run fn later in a copy of the context taken now
    context = contextvars.copy_context()
    return lambda *a, **k: context.copy().run(fn, *a, **k)


def _import_warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "could not set up its thread-birth hook" in r.getMessage()]


def _exactly_one_import_warning(caplog):
    """The import-time WARNING is written at once, in the installing thread (no thread, no queue)."""
    warnings = _import_warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def clean_thread(target):
    return threading.Thread(target=target, context=contextvars.Context()) if NEW else threading.Thread(target=target)


def isolated(fn):  # run fn in a fresh thread with an empty context
    out = {}

    def run():
        try:
            out["v"] = fn()
        except Exception as error:  # noqa: BLE001
            out["v"] = f"EXC {error!r}"
    thread = clean_thread(run)
    thread.start()
    thread.join(30)
    return out["v"]


def stale_pool(name):
    """A one-worker pool whose worker is born inside request `name`; that request stays open."""
    pool = cf.ThreadPoolExecutor(1)
    release, ready = threading.Event(), threading.Event()

    def request():
        with scope(name):
            pool.submit(lambda: None).result()
            ready.set()
            release.wait()
    thread = clean_thread(request)
    thread.start()
    ready.wait()

    def done():
        release.set()
        thread.join()
        pool.shutdown()
    return pool, done


def with_c(body):  # request A keeps a stale worker; request C uses it
    pool, done = stale_pool("A")
    try:
        def c():
            with scope("C"):
                return body(pool)
        return isolated(c)
    finally:
        done()


async def _probe_async():
    return probe()


def _m1():
    pool = cf.ThreadPoolExecutor(1)
    with scope("A"):
        return pool.submit(probe).result()


def _m2():
    pool = cf.ThreadPoolExecutor(1)
    with scope("A"):
        return pool.submit(contextvars.copy_context().run, probe).result()


def _to_thread(times):
    async def main():
        with scope("T"):
            result = await asyncio.to_thread(probe)
            if times == 2:
                result = await asyncio.to_thread(probe)
            return result
    return asyncio.run(main())


def _second_request(pattern):  # T's request bears the worker; U's request uses it
    async def main():
        release, born = asyncio.Event(), asyncio.Event()

        async def t():
            with scope("T"):
                await asyncio.to_thread(lambda: None)
                born.set()
                await release.wait()

        async def u():
            await born.wait()
            with scope("U"):
                try:
                    return await pattern()
                finally:
                    release.set()
        task = asyncio.create_task(t())
        result = await u()
        await task
        return result
    return asyncio.run(main())


def _anyio_second(pattern):
    async def main():
        release, born, out = anyio.Event(), anyio.Event(), {}

        async def v():
            with scope("V"):
                await anyio.to_thread.run_sync(lambda: None)
                born.set()
                await release.wait()

        async def w():
            await born.wait()
            with scope("W"):
                out["r"] = await pattern()
                release.set()
        async with anyio.create_task_group() as group:
            group.start_soon(v)
            group.start_soon(w)
        return out["r"]
    return anyio.run(main)


def _anyio_first():
    async def main():
        with scope("V"):
            return await anyio.to_thread.run_sync(probe)
    return anyio.run(main)


def _l1(pool):
    def work():
        second = cf.ThreadPoolExecutor(1)
        return second.submit(contextvars.copy_context().run, probe).result()
    return pool.submit(work).result()


def _l2(pool):
    async def inner():
        return await asyncio.to_thread(probe)
    return pool.submit(lambda: asyncio.run(inner())).result()


def _l3(pool):
    async def inner():
        return await anyio.to_thread.run_sync(probe)
    return pool.submit(lambda: anyio.run(inner)).result()


def _l4(pool):
    def work():
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("r", probe()))
        thread.start()
        thread.join()
        return out["r"]
    return pool.submit(work).result()


def _l5(pool):
    context = contextvars.copy_context()
    return pool.submit(lambda: context.run(probe)).result()


def _thread_handler(loop, func, *args):  # asgiref's SyncToAsync.thread_handler shape
    return func(*args)


def _l6():
    async def main():
        pool = cf.ThreadPoolExecutor(1)
        loop = asyncio.get_running_loop()
        with scope("D"):
            context = contextvars.copy_context()
            return await loop.run_in_executor(pool, functools.partial(_thread_handler, loop, context.run, probe))
    return asyncio.run(main())


def _l7():
    async def main():
        with scope("T"):
            def work():
                second = cf.ThreadPoolExecutor(1)
                return second.submit(contextvars.copy_context().run, probe).result()
            return await asyncio.to_thread(work)
    return asyncio.run(main())


def _l9(pool):  # the executor's work item rewritten (a future Python): an explicit hand-over still works
    old = cft._WorkItem

    class _WorkItem:
        def __init__(self, future, *task):
            self.future = future
            self.task = task

        def run(self, *args):
            if not self.future.set_running_or_notify_cancel():
                return
            fn, a, k = self.task if len(self.task) == 3 else self.task[0]
            try:
                result = fn(*a, **k)
            except BaseException as error:
                self.future.set_exception(error)
            else:
                self.future.set_result(result)
    cft._WorkItem = _WorkItem
    try:
        return pool.submit(contextvars.copy_context().run, probe).result()
    finally:
        cft._WorkItem = old


def _l10():
    async def main():
        loop = asyncio.get_running_loop()
        release, born = asyncio.Event(), asyncio.Event()

        async def t():
            with scope("T"):
                await asyncio.to_thread(lambda: None)
                born.set()
                await release.wait()

        async def u():
            await born.wait()
            with scope("U"):
                try:
                    return await loop.run_in_executor(
                        None, lambda: asyncio.run_coroutine_threadsafe(_probe_async(), loop).result())
                finally:
                    release.set()
        task = asyncio.create_task(t())
        result = await u()
        await task
        return result
    return asyncio.run(main())


def _l11():
    async def main():
        loop = asyncio.get_running_loop()
        jobs, release, ready = queue.Queue(), asyncio.Event(), asyncio.Event()

        def consumer():
            while True:
                job = jobs.get()
                if job is None:
                    return
                job()

        async def a():
            with scope("A"):
                threading.Thread(target=consumer, daemon=True).start()
                ready.set()
                await release.wait()

        async def b():
            await ready.wait()
            with scope("B"):
                future = loop.create_future()

                def job():
                    result = asyncio.run_coroutine_threadsafe(_probe_async(), loop).result()
                    loop.call_soon_threadsafe(future.set_result, result)
                jobs.put(job)
                try:
                    return await future
                finally:
                    release.set()
                    jobs.put(None)
        task = asyncio.create_task(a())
        result = await b()
        await task
        return result
    return asyncio.run(main())


def _l12():
    with scope("C"):
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("r", probe()), context=contextvars.copy_context())
        thread.start()
        thread.join()
        return out["r"]


def _l13():
    with scope("C"):
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("r", probe()))
        thread.start()
        thread.join()
        return out["r"]


CASES = [
    ("M1 A plain submit (worker born in A)", {"A", "HATA"}, lambda: isolated(_m1)),
    ("M2 A submit(copy_context().run)", {"A"}, lambda: isolated(_m2)),
    ("M3 C plain submit to A's stale worker", {"HATA"}, lambda: with_c(lambda p: p.submit(probe).result())),
    ("M4 C plain submit + asyncio.run inside", {"HATA"},
     lambda: with_c(lambda p: p.submit(lambda: asyncio.run(_probe_async())).result())),
    ("M5 C plain submit + copy_context().run inside", {"HATA"},
     lambda: with_c(lambda p: p.submit(lambda: contextvars.copy_context().run(probe)).result())),
    ("M6 C submit(copy_context().run) explicit hand-over", {"C"},
     lambda: with_c(lambda p: p.submit(contextvars.copy_context().run, probe).result())),
    ("M7 C submit(bind(work))", {"C"}, lambda: with_c(lambda p: p.submit(bind(probe)).result())),
    ("M8 asyncio.to_thread first (worker born in T)", {"T"}, lambda: isolated(lambda: _to_thread(1))),
    ("M9 asyncio.to_thread twice in one request", {"T"}, lambda: isolated(lambda: _to_thread(2))),
    ("M10 asyncio.to_thread, another request, same worker", {"U"},
     lambda: isolated(lambda: _second_request(lambda: asyncio.to_thread(probe)))),
    ("M11 anyio run_sync first", {"V"}, lambda: isolated(_anyio_first)),
    ("M12 anyio run_sync, another request, same worker", {"W"},
     lambda: isolated(lambda: _anyio_second(lambda: anyio.to_thread.run_sync(probe)))),
    ("M13 anyio run_sync(bind(work))", {"W"},
     lambda: isolated(lambda: _anyio_second(lambda: anyio.to_thread.run_sync(bind(probe))))),
    ("L1 stale worker, inside: second.submit(copy_context().run)", {"HATA"}, lambda: with_c(_l1)),
    ("L2 stale worker, inside: asyncio.run(to_thread)", {"HATA"}, lambda: with_c(_l2)),
    ("L3 stale worker, inside: anyio.run(run_sync)", {"HATA"}, lambda: with_c(_l3)),
    ("L4 stale worker, inside: threading.Thread", {"HATA"}, lambda: with_c(_l4)),
    ("L5 C submit(lambda: ctx.run(f))", {"C"}, lambda: with_c(_l5)),
    ("L6 asgiref shape run_in_executor(partial(thread_handler, ctx.run))", {"D"}, lambda: isolated(_l6)),
    ("L7 to_thread work, inside: second.submit(ctx.run)", {"T"}, lambda: isolated(_l7)),
    ("L8 U: run_in_executor(None, f) plain, worker born in T", {"HATA"},
     lambda: isolated(lambda: _second_request(lambda: asyncio.get_running_loop().run_in_executor(None, probe)))),
    ("L9 rewritten _WorkItem + C submit(ctx.run)", {"C"}, lambda: with_c(_l9)),
    ("L10 stale worker -> run_coroutine_threadsafe back to the loop", {"HATA"}, lambda: isolated(_l10)),
    ("L11 A's own consumer thread, B's job -> back to the loop", {"HATA"}, lambda: isolated(_l11)),
    ("L13 C plain threading.Thread child", {"C", "HATA"}, lambda: isolated(_l13)),
]
if NEW:
    CASES.append(("L12 C Thread(context=copy_context()) explicit hand-over", {"C"}, lambda: isolated(_l12)))


@pytest.mark.parametrize("name,expected,run", CASES, ids=[c[0].split(" ")[0] for c in CASES])
def test_a_thread_never_serves_another_request_s_scope(name, expected, run):
    got = run()
    assert got in expected, f"{name}: got {got!r}, correct is one of {sorted(expected)}"


def test_on_3_14t_a_plain_thread_child_sees_no_scope_as_on_3_11():
    assert isolated(_l13) == "HATA"


def test_the_birth_cleaning_is_active_exactly_where_threads_inherit():
    assert _thread_birth.STATUS == ("active" if INHERITS else "not-needed")


# ---- the real FastAPI bridge: one request's worker never serves another request's session ----

def test_real_fastapi_routes_keep_their_own_session():
    fastapi = pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from py_autowired import activate_container
    from py_autowired.fastapi import attach_container

    numbers = itertools.count(1)

    class Session:
        def __init__(self):
            self.no = next(numbers)

    own = Container()
    own.register_scoped(Session)
    app = fastapi.FastAPI()
    attach_container(app, own)
    activate_container(own)  # its container is the active one (another test may have left another)

    def resolve():
        try:
            return own.resolve(Session).no
        except Exception as error:  # noqa: BLE001
            return type(error).__name__

    def verdict(mine, got):
        return got if isinstance(got, str) else ("own" if got == mine else f"OTHER({got})")

    release, born = asyncio.Event(), asyncio.Event()

    @app.get("/stale")
    async def stale():
        resolve()
        await asyncio.to_thread(lambda: None)  # the default pool's worker is born in this request
        born.set()
        await release.wait()
        return {}

    @app.get("/plain")
    async def plain():
        mine = resolve()
        return {"r": verdict(mine, await asyncio.get_running_loop().run_in_executor(None, resolve))}

    async def back():
        return resolve()

    @app.get("/back")
    async def back_to_loop():
        mine, loop = resolve(), asyncio.get_running_loop()
        got = await loop.run_in_executor(None, lambda: asyncio.run_coroutine_threadsafe(back(), loop).result())
        return {"r": verdict(mine, got)}

    @app.get("/to_thread")
    async def to_thread():
        mine = resolve()
        return {"r": verdict(mine, await asyncio.to_thread(resolve))}

    def dependency():
        return resolve()

    @app.get("/sync")
    def sync(mine=fastapi.Depends(dependency)):
        return {"r": verdict(mine, resolve())}

    @app.get("/asyncio_run_inside")
    async def asyncio_run_inside():
        mine, loop = resolve(), asyncio.get_running_loop()
        return {"r": verdict(mine, await loop.run_in_executor(None, lambda: asyncio.run(back())))}

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            holder = asyncio.create_task(client.get("/stale"))
            await asyncio.wait_for(born.wait(), 10)  # a failing /stale must not hang the suite
            out = {path: (await client.get(path)).json()["r"]
                   for path in ("/plain", "/back", "/asyncio_run_inside", "/to_thread", "/sync")}
            release.set()
            await holder
            return out

    assert asyncio.run(main()) == {
        "/plain": "ScopeNotActiveError", "/back": "ScopeNotActiveError",
        "/asyncio_run_inside": "ScopeNotActiveError", "/to_thread": "own", "/sync": "own"}


# ---- installation, self-test, fall-back, other monitoring tools ----

needs_inherit = pytest.mark.skipif(not INHERITS, reason="threads inherit their context only on this build")


@pytest.fixture
def reinstalled():
    """Forget the import's decision (tests only), let the test occupy tool ids, then decide again."""
    taken = []
    _thread_birth._reset_for_tests()
    yield taken
    for tool in taken:
        sys.monitoring.free_tool_id(tool)
    _thread_birth._reset_for_tests()
    assert _thread_birth._install() == "active"
    assert isolated(lambda: _to_thread(1)) == "T", "the hard rule stayed after the test's reset"


def test_on_a_build_without_inheritance_nothing_is_installed():
    if INHERITS:
        pytest.skip("this build inherits: the hook is needed")
    assert _thread_birth.STATUS == "not-needed"
    assert _thread_birth._install() == "not-needed"
    assert _thread_birth._tool is None


@needs_inherit
def test_a_free_tool_id_is_used_when_the_first_choice_is_taken(reinstalled):
    first = _thread_birth._TOOL_IDS[0]
    sys.monitoring.use_tool_id(first, "someone else")
    reinstalled.append(first)
    assert _thread_birth._install() == "active"
    assert _thread_birth._tool != first
    assert isolated(_l13) == "HATA"


@needs_inherit
def test_without_a_free_tool_id_it_falls_back_to_the_hard_rule_and_says_so_once(reinstalled, caplog):
    for tool in _thread_birth._TOOL_IDS:
        sys.monitoring.use_tool_id(tool, "someone else")
        reinstalled.append(tool)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        assert _thread_birth._install() == "hard-rule"
        _exactly_one_import_warning(caplog)  # written at once, before _install returns
        assert _thread_birth._install() == "hard-rule"
        _exactly_one_import_warning(caplog)
    # never silent: a scope handed to another thread by inheritance is refused, the own thread works
    assert isolated(_l13) == "HATA"
    assert with_c(lambda p: p.submit(probe).result()) == "HATA"
    with scope("C"):
        assert probe() == "C"
    # no thread is trusted by its name - an anyio worker is refused too (explicitly)
    assert isolated(_anyio_first) == "HATA"


@needs_inherit
def test_a_failing_self_test_falls_back_too(reinstalled, monkeypatch, caplog):
    monkeypatch.setattr(_thread_birth, "_self_test", lambda: False)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        assert _thread_birth._install() == "hard-rule"
        _exactly_one_import_warning(caplog)
    assert _thread_birth._tool is None, "the tool id was kept after a failed self-test"


@needs_inherit
def test_a_debugger_using_sys_monitoring_works_alongside():
    import bdb

    lines = []

    class Recorder(bdb.Bdb):
        def user_line(self, frame):
            if frame.f_code.co_name == "traced":
                lines.append(frame.f_lineno)

    def traced():
        with scope("C"):
            out = {}
            thread = threading.Thread(target=lambda: out.setdefault("r", probe()))
            thread.start()
            thread.join()
            return out["r"]

    recorder = Recorder(backend="monitoring")
    result = isolated(lambda: recorder.runcall(traced))
    assert lines, "the debugger saw no line of the traced function"
    assert result == "HATA", "the birth cleaning stopped working under the debugger"
    assert _thread_birth.STATUS == "active"


@needs_inherit
def test_another_tool_s_events_on_thread_start_still_fire():
    tool = sys.monitoring.COVERAGE_ID
    if sys.monitoring.get_tool(tool) is not None:
        pytest.skip("the coverage id is in use")
    calls = []
    sys.monitoring.use_tool_id(tool, "coverage-like")
    try:
        sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START,
                                         lambda code, offset: calls.append(code.co_name))
        sys.monitoring.set_local_events(tool, threading.Thread.start.__code__, sys.monitoring.events.PY_START)
        assert isolated(_l13) == "HATA"
        assert "start" in calls, "the other tool's callback did not run"
    finally:
        sys.monitoring.set_local_events(tool, threading.Thread.start.__code__, 0)
        sys.monitoring.register_callback(tool, sys.monitoring.events.PY_START, None)
        sys.monitoring.free_tool_id(tool)


@needs_inherit
def test_the_real_self_test_catches_a_hook_that_does_not_clean(reinstalled, monkeypatch, caplog):
    monkeypatch.setattr(_thread_birth, "_on_thread_start", lambda code, offset: None)  # a broken hook
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        assert _thread_birth._install() == "hard-rule"
    assert isolated(_l13) == "HATA"  # never silent


@needs_inherit
def test_the_self_test_itself_tells_a_healthy_hook_from_one_that_cleans_too_much(monkeypatch):
    assert _thread_birth._self_test() is True  # the installed hook keeps other values, drops ours
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(lambda: contextvars.Context()))
    assert _thread_birth._self_test() is False, "a hook that drops every value passed the self-test"


@needs_inherit
def test_a_hook_that_cleans_too_much_is_never_kept(reinstalled, monkeypatch, caplog):
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(lambda: contextvars.Context()))
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        assert _thread_birth._install() == "hard-rule"
        _exactly_one_import_warning(caplog)
    assert _thread_birth._tool is None, "the tool id was kept for a hook that failed its self-test"


@needs_inherit
def test_a_plain_thread_still_inherits_every_value_that_is_not_the_library_s():
    other = contextvars.ContextVar("user_request_id", default=None)

    def run():
        other.set("r-42")
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("v", other.get()))
        thread.start()
        thread.join()
        return out["v"]
    assert contextvars.Context().run(run) == "r-42", "the birth cleaning dropped another library's value"


# ---- the hard rule: a scope is read only in the thread that entered it ----

def _force_fall_back(taken):
    for tool in _thread_birth._TOOL_IDS:
        sys.monitoring.use_tool_id(tool, "someone else")
        taken.append(tool)
    assert _thread_birth._install() == "hard-rule"


@needs_inherit
def test_fall_back_a_scope_entered_in_another_thread_belongs_to_that_thread(reinstalled):
    _force_fall_back(reinstalled)
    made = container.create_scope()  # created here, entered in another thread
    out = {}

    def enter_there():
        with made:
            try:
                container.resolve(Tag)
                out["v"] = "ok"
            except ScopeNotActiveError:
                out["v"] = "refused"
    isolated(enter_there)
    assert out["v"] == "ok", "an explicitly entered scope was refused in its own thread"


@needs_inherit
def test_fall_back_a_thread_name_does_not_open_another_thread_s_scope(reinstalled):
    _force_fall_back(reinstalled)
    out = {}

    def request():
        with scope("A"):
            worker = threading.Thread(target=lambda: out.setdefault("v", probe()),
                                      name="AnyIO worker thread (an ordinary one)")
            worker.start()
            worker.join()
    isolated(request)
    assert out["v"] == "HATA", "a thread got another thread's scope because of its name"


@needs_inherit
def test_fall_back_never_hands_a_scope_to_another_thread_silently(reinstalled):
    _force_fall_back(reinstalled)
    assert isolated(lambda: _to_thread(1)) == "HATA"  # refused explicitly (documented), never wrong
    assert isolated(_anyio_first) == "HATA"


class _ReusedIdent:
    """threading as the library sees it, after the OS gave a finished thread's number to a new one."""

    def __init__(self, ident):
        self._ident = ident

    def get_ident(self):
        return self._ident

    def __getattr__(self, name):
        return getattr(threading, name)


@needs_inherit
def test_fall_back_a_reused_thread_number_never_opens_a_finished_thread_s_scope(reinstalled, monkeypatch):
    """Review round 6: A enters a scope and ends; B gets A's thread number and runs A's context."""
    _force_fall_back(reinstalled)
    made = container.create_scope()
    seen = {}

    def a():
        made.__enter__()  # A's scope, still open when A ends
        container.resolve(Tag).name = "A"
        seen["owner"] = probe()  # the owner itself reads it
        seen["context"] = contextvars.copy_context()
        seen["ident"] = threading.get_ident()
    isolated(a)
    assert seen["owner"] == "A", "the owner could not read its own scope"
    monkeypatch.setattr(container_module, "threading", _ReusedIdent(seen["ident"]))
    out = {}
    b = threading.Thread(target=lambda: out.setdefault("v", probe()), context=seen["context"])
    b.start()
    b.join()
    assert out["v"] == "HATA", "a new thread with a finished thread's number read its scope"


@needs_inherit
def test_after_a_fall_back_it_never_returns_to_active(reinstalled):
    _force_fall_back(reinstalled)
    for tool in reinstalled:  # the tool ids are free again
        sys.monitoring.free_tool_id(tool)
    reinstalled.clear()
    assert _thread_birth._install() == "hard-rule", "a fall-back was undone without a restart"
    assert _thread_birth._tool is None
    assert isolated(lambda: _to_thread(1)) == "HATA"


@needs_inherit
def test_a_self_test_that_cannot_start_its_thread_falls_back_instead_of_breaking_the_import(
        reinstalled, monkeypatch, caplog):
    real_start = threading.Thread.start

    def refusing(self):
        if self.name == "py_autowired thread-birth self-test":
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", refusing)
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        try:
            status = _thread_birth._install()
        except Exception as error:  # noqa: BLE001 - the import must not break
            status = f"raised {error!r}"
    assert status == "hard-rule", status
    assert _thread_birth._tool is None, "the tool id leaked after a failed self-test"


@needs_inherit
def test_a_self_test_whose_thread_never_runs_does_not_hang_the_import(reinstalled):
    release = threading.Event()

    def blocking_trace(frame, event, arg):  # e.g. a user trace hook importing the package in a new thread
        if threading.current_thread().name == "py_autowired thread-birth self-test":
            release.wait(30)
        return None
    threading.settrace(blocking_trace)
    result = {}
    try:
        installer = clean_thread(lambda: result.setdefault("status", _thread_birth._install()))
        installer.start()
        installer.join(15)
        hung = installer.is_alive()
    finally:
        threading.settrace(None)
        release.set()
    assert not hung, "installing waited for a self-test thread that never ran"
    assert result["status"] == "hard-rule"


@needs_inherit
def test_a_thread_start_without_code_falls_back_and_frees_the_tool(reinstalled, monkeypatch):
    class Wrapped:  # a wrapper around Thread.start that has no __code__ (e.g. from another tool)
        def __init__(self, inner):
            self.inner = inner

        def __get__(self, obj, owner=None):
            return functools.partial(self.inner, obj) if obj is not None else self

    monkeypatch.setattr(threading.Thread, "start", Wrapped(threading.Thread.start))
    try:
        status = _thread_birth._install()
    except Exception as error:  # noqa: BLE001 - the import must not break
        status = f"raised {error!r}"
    assert status == "hard-rule", status
    assert _thread_birth._tool is None, "the tool id was kept after a failed installation"
    assert not [i for i in range(6) if sys.monitoring.get_tool(i) == _thread_birth._TOOL_NAME]


@needs_inherit
def test_a_broken_log_handler_never_breaks_the_installation(reinstalled, monkeypatch):
    class Failing(logging.Handler):
        def handle(self, record):
            raise RuntimeError("the log handler is broken")
    monkeypatch.setattr(_thread_birth, "_self_test", lambda: False)
    handler = Failing()
    log = logging.getLogger("py_autowired")
    log.addHandler(handler)
    try:
        try:
            status = _thread_birth._install()
        except Exception as error:  # noqa: BLE001 - the import must not break
            status = f"raised {error!r}"
    finally:
        log.removeHandler(handler)
    assert status == "hard-rule", status


@needs_inherit
def test_a_hook_failure_during_the_installation_s_self_test_is_never_undone(reinstalled, monkeypatch):
    """Review round 8: while the import's self-test runs, the hook fails in another thread (it
    switches to the hard rule). The self-test then passes - the installation must not turn the
    status back to "active": a thread that passed the failing hook may hold an inherited scope."""
    real_clean = _thread_birth._clean_copy
    real_self_test = _thread_birth._self_test

    def clean(*args):
        if threading.current_thread().name == "starts a thread meanwhile":
            raise RuntimeError("frame access refused")
        return real_clean(*args)

    def self_test_while_another_thread_starts_one():
        def start_one():
            child = threading.Thread(target=lambda: None)
            child.start()  # the hook fails here: the hard rule takes over
            child.join()
        other = threading.Thread(target=start_one, name="starts a thread meanwhile")
        other.start()
        other.join(10)
        assert _thread_birth.STATUS == "hard-rule"
        return real_self_test()  # passes: the probe thread's birth is cleaned normally
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(clean))
    monkeypatch.setattr(_thread_birth, "_self_test", staticmethod(self_test_while_another_thread_starts_one))
    status = _thread_birth._install()
    assert status == "hard-rule" and _thread_birth.STATUS == "hard-rule", "a fall-back was undone"
    monkeypatch.undo()
    assert isolated(lambda: _to_thread(1)) == "HATA", "the hard rule was lifted"


@needs_inherit
def test_the_installation_s_success_waits_for_a_fall_back_already_under_way(reinstalled, monkeypatch):
    """Review round 8: the hook's fall-back is half done (the hard rule set, the status not yet)
    when the self-test passes. The success must wait for it, not lift the rule in between."""
    real_clean = _thread_birth._clean_copy
    real_self_test = _thread_birth._self_test
    real_apply = _thread_birth._apply_hard_rule
    in_fall_back = threading.Event()

    def clean(*args):
        if threading.current_thread().name == "falls back slowly":
            raise RuntimeError("frame access refused")
        return real_clean(*args)

    def apply_slowly():
        real_apply()
        if threading.current_thread().name == "falls back slowly":
            in_fall_back.set()
            threading.Event().wait(0.5)  # the status is written after this pause

    def self_test_during_a_fall_back():
        def start_one():
            child = threading.Thread(target=lambda: None)
            child.start()
            child.join()
        other = threading.Thread(target=start_one, name="falls back slowly", daemon=True)
        other.start()
        assert in_fall_back.wait(10)
        return real_self_test()
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(clean))
    monkeypatch.setattr(_thread_birth, "_apply_hard_rule", staticmethod(apply_slowly))
    monkeypatch.setattr(_thread_birth, "_self_test", staticmethod(self_test_during_a_fall_back))
    _thread_birth._install()
    monkeypatch.undo()
    assert _thread_birth.STATUS == "hard-rule"
    assert isolated(lambda: _to_thread(1)) == "HATA", "the hard rule was lifted under a hard-rule status"


@needs_inherit
def test_two_copies_claiming_tool_ids_at_the_same_time_are_never_both_active(reinstalled, monkeypatch):
    """Review round 9: two copies of the package install at the same moment - both find the name
    free, then each takes its own id (4 and 3). Only one may be "active"; here the other copy
    claims its id right after ours, so this one sees it and keeps the hard rule."""
    real_use = sys.monitoring.use_tool_id
    other = [i for i in _thread_birth._TOOL_IDS][1]

    def use_tool_id(tool, name):
        real_use(tool, name)
        if name == _thread_birth._TOOL_NAME and not reinstalled:
            real_use(other, name)          # the other copy, at the same moment
            reinstalled.append(other)
    monkeypatch.setattr(sys.monitoring, "use_tool_id", use_tool_id)
    status = _thread_birth._install()
    monkeypatch.undo()
    assert status == "hard-rule", "two copies were active at once"
    assert _thread_birth._tool is None, "the yielding copy kept its tool id"
    assert isolated(lambda: _to_thread(1)) == "HATA"


@needs_inherit
def test_a_fall_back_during_the_installation_still_gives_the_import_warning(reinstalled, monkeypatch, caplog):
    """Review round 10: the hook failed in another thread while the import installed it; the
    installation ends in the hard rule - like any fall-back at import, it says so once."""
    real_clean = _thread_birth._clean_copy
    real_self_test = _thread_birth._self_test

    def clean(*args):
        if threading.current_thread().name == "starts a thread meanwhile":
            raise RuntimeError("frame access refused")
        return real_clean(*args)

    def self_test_while_another_thread_starts_one():
        other = threading.Thread(target=lambda: threading.Thread(target=lambda: None).start(),
                                 name="starts a thread meanwhile")
        other.start()
        other.join(10)
        return real_self_test()
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(clean))
    monkeypatch.setattr(_thread_birth, "_self_test", staticmethod(self_test_while_another_thread_starts_one))
    with caplog.at_level(logging.WARNING, logger="py_autowired"):
        assert _thread_birth._install() == "hard-rule"
        _exactly_one_import_warning(caplog)


# ---- a failure of the hook while the program runs ----

_REASON = "STATUS=hard-rule"


def _refusal_message():
    """The message a refused read gives in a plain child thread of a request."""
    def request():
        with scope("A"):
            box = {}

            def read():
                try:
                    container.resolve(Tag)
                    box["v"] = "read"
                except ScopeNotActiveError as error:
                    box["v"] = str(error)
            worker = threading.Thread(target=read)
            worker.start()
            worker.join()
            return box["v"]
    return isolated(request)


@needs_inherit
def test_a_failing_birth_hook_switches_to_the_hard_rule_and_the_refusal_says_why(reinstalled, monkeypatch, caplog):
    assert _thread_birth._install() == "active"

    def broken():
        raise RuntimeError("frame access refused")
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(broken))
    with caplog.at_level(logging.DEBUG, logger="py_autowired"):
        message = _refusal_message()  # the hook fails at this child's birth
        assert _thread_birth.STATUS == "hard-rule"
        assert _REASON in message and "Known limits" in message, message
        assert "asyncio.to_thread" in message, message
        assert not [r for r in caplog.records if r.name.startswith("py_autowired")], \
            [r.getMessage() for r in caplog.records]
    monkeypatch.undo()
    assert isolated(lambda: _to_thread(1)) == "HATA"  # one way: it stays, the hook works again or not
    assert _thread_birth._install() == "hard-rule"


@needs_inherit
def test_a_refusal_without_a_fall_back_does_not_blame_the_hook():
    assert _thread_birth.STATUS == "active"
    message = _refusal_message()
    assert message == "read" or "STATUS" not in message, message
    assert _thread_birth._install() == "active"


@needs_inherit
def test_a_failing_hook_never_logs_in_the_starting_thread(reinstalled, monkeypatch):
    assert _thread_birth._install() == "active"  # the hook is installed: its failure is what is tested
    held = threading.Lock()
    records = []

    class NeedsTheLock(logging.Handler):  # e.g. a handler that needs a lock the starter holds
        def handle(self, record):  # without logging's own handler lock: a hang never blocks the exit
            with held:
                records.append(record.getMessage())
            return True
    handler = NeedsTheLock()
    log = logging.getLogger("py_autowired")
    log.addHandler(handler)

    def broken():
        raise RuntimeError("frame access refused")
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(broken))
    done = threading.Event()
    queued = []
    monkeypatch.setattr(container_module, "_WARNED", set())  # no once-only mark hides a log line
    monkeypatch.setattr(container_module._BACKGROUND_LOG, "put", lambda *a, **k: queued.append(a))

    def starter():
        with held:  # the starting thread holds the lock while it starts a thread
            child = threading.Thread(target=lambda: None)
            child.start()
            child.join()
        done.set()
    try:
        runner = threading.Thread(target=starter, context=contextvars.Context(), daemon=True)
        runner.start()
        assert done.wait(10), "the hook logged while its thread held a lock the handler needs (deadlock)"
    finally:
        log.removeHandler(handler)
    assert _thread_birth.STATUS == "hard-rule"
    assert records == [], records
    assert queued == [], "the hook handed a log line to the background writer"


# ---- other copies of the package (discovery may import the library's own source a second time) ----

def _load_second_copy(name):
    import importlib.util
    from pathlib import Path
    package_dir = Path(sys.modules["py_autowired"].__file__).parent
    spec = importlib.util.spec_from_file_location(name, package_dir / "__init__.py",
                                                  submodule_search_locations=[str(package_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _drop_second_copy(name):
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        second = sys.modules.pop(key)
        birth = getattr(second, "_ThreadBirth", None)
        if birth is not None:
            birth._reset_for_tests()


def _second_copy_refuses_a_handed_scope(module):
    other = module.Container()
    other.register_scoped(Tag)

    def handed():
        with other.create_scope():
            other.resolve(Tag).name = "A"
            box = {}

            def read():
                try:
                    box["v"] = other.resolve(Tag).name
                except module.ScopeNotActiveError:
                    box["v"] = "HATA"
            worker = threading.Thread(target=read, context=contextvars.copy_context())
            worker.start()
            worker.join()
            return box["v"]
    return isolated(handed)


def _owner_hands_over():
    """A scope of this module's container, handed to another thread explicitly."""
    def handed():
        with scope("A"):
            box = {}
            worker = threading.Thread(target=lambda: box.setdefault("v", probe()), context=contextvars.copy_context())
            worker.start()
            worker.join()
            return box["v"]
    return isolated(handed)


@needs_inherit
def test_a_second_copy_takes_no_second_tool_id_and_keeps_the_hard_rule(caplog):
    name = "py_autowired_second_copy_for_test"
    try:
        with caplog.at_level(logging.WARNING, logger="py_autowired"):
            module = _load_second_copy(name)
        ours = [i for i in range(6) if sys.monitoring.get_tool(i) == _thread_birth._TOOL_NAME]
        assert len(ours) == 1, f"tool ids taken by the library: {ours}"
        assert module._thread_birth.STATUS == "hard-rule"
        assert module._thread_birth._tool is None
        _exactly_one_import_warning(caplog)
        assert _second_copy_refuses_a_handed_scope(module) == "HATA", "the second copy read silently"
        assert _owner_hands_over() == "A" and _thread_birth.STATUS == "active"  # the owner is untouched
    finally:
        _drop_second_copy(name)


@needs_inherit
def test_a_copy_loaded_after_a_runtime_fall_back_stays_in_the_hard_rule(reinstalled, monkeypatch):
    assert _thread_birth._install() == "active"
    monkeypatch.setattr(_thread_birth, "_clean_copy", staticmethod(lambda: 1 / 0))
    assert isolated(_l13) == "HATA"  # the hook fails here
    monkeypatch.undo()
    assert _thread_birth.STATUS == "hard-rule"
    name = "py_autowired_second_copy_late"
    try:
        module = _load_second_copy(name)
        assert module._thread_birth.STATUS == "hard-rule"
        assert _second_copy_refuses_a_handed_scope(module) == "HATA"
    finally:
        _drop_second_copy(name)


@needs_inherit
def test_during_a_real_installation_the_installer_holds_the_hard_rule_and_a_new_copy_is_not_active(
        reinstalled, monkeypatch):
    """A second copy loaded in the middle of an installation (between taking the tool id and the
    finished check) is not active, and the installing copy itself keeps the hard rule till then."""
    seen = {}
    name = "py_autowired_second_copy_mid_install"
    real_self_test = _thread_birth._self_test

    def self_test_loading_a_copy():
        module = _load_second_copy(name)
        seen["status"] = module._thread_birth.STATUS
        seen["owner"] = _owner_hands_over()
        return real_self_test()
    monkeypatch.setattr(_thread_birth, "_self_test", staticmethod(self_test_loading_a_copy))
    try:
        assert _thread_birth._install() == "active"
        assert seen["status"] == "hard-rule", "a copy loaded mid-installation was taken as active"
        assert seen["owner"] == "HATA", "the installing copy read a handed scope without the hard rule"
        assert sys.modules[name]._thread_birth.STATUS == "hard-rule"  # and stays so
        assert _owner_hands_over() == "A"
    finally:
        _drop_second_copy(name)


# ---- a clean process ----

def _run_clean(code):
    import os
    import subprocess
    from pathlib import Path

    src = str(Path(sys.modules["py_autowired"].__file__).parent.parent)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": src})


_HANDED = (
    "from py_autowired import Container, ScopeNotActiveError\n"
    "class Tag: pass\n"
    "c = Container(); c.register_scoped(Tag)\n"
    "out = {}\n"
    "with c.create_scope():\n"
    "    c.resolve(Tag)\n"
    "    def read():\n"
    "        try:\n"
    "            c.resolve(Tag); out['v'] = 'A'\n"
    "        except ScopeNotActiveError:\n"
    "            out['v'] = 'HATA'\n"
    "    t = threading.Thread(target=read, context=contextvars.copy_context()); t.start(); t.join()\n"
)


@needs_inherit
def test_a_tool_id_taken_under_the_library_s_name_before_import_is_not_active():
    """Foreign code took a tool id under the library's name before the import: no copy of the library
    set the hook up, so the status is "hard-rule" and the WARNING is given at import."""
    result = _run_clean(
        "import sys, logging, threading, contextvars\n"
        "logging.basicConfig(level=logging.WARNING)\n"
        "sys.monitoring.use_tool_id(4, 'py_autowired thread birth')\n"
        "import py_autowired\n"
        + _HANDED +
        "print('STATUS', py_autowired._thread_birth.STATUS, 'HANDED', out['v'])\n")
    assert result.returncode == 0, result.stderr
    assert "STATUS hard-rule HANDED HATA" in result.stdout, result.stdout
    assert "could not set up its thread-birth hook" in result.stderr, "no WARNING for the hard rule"


@needs_inherit
def test_an_import_while_no_thread_can_start_falls_back_without_a_thread_or_a_queue():
    result = _run_clean(
        "import sys, logging, threading, contextvars\n"
        "logging.basicConfig(level=logging.WARNING)\n"
        "real_start = threading.Thread.start\n"
        "tried = []\n"
        "def refusing(self):\n"
        "    tried.append(self.name)\n"
        "    raise RuntimeError(\"can't start new thread\")\n"
        "threading.Thread.start = refusing\n"
        "import queue\n"
        "puts = []\n"
        "real_put = queue.Queue.put_nowait\n"
        "queue.Queue.put_nowait = lambda self, item: (puts.append(item), real_put(self, item))[1]\n"
        "import py_autowired\n"
        "from py_autowired import container as m\n"
        "print('STATUS', py_autowired._thread_birth.STATUS, 'TRIED', tried, 'QUEUED', len(puts),\n"
        "      m._BACKGROUND_LOG._queue.qsize())\n")
    assert result.returncode == 0, result.stderr
    # the only thread ever tried is the self-test's probe: no writer thread, nothing queued
    assert "STATUS hard-rule TRIED ['py_autowired thread-birth self-test'] QUEUED 0 0" in result.stdout, \
        result.stdout
    assert "could not set up its thread-birth hook" in result.stderr, "no WARNING at import"
