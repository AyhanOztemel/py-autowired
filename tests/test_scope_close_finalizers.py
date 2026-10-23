"""0.5.4: a closing scope never hands out an object that is already disposed, and never hands out
its objects to anyone but the code that is closing it.

P, Q and R are scoped and created in this order, so they are finalized R, Q, P. While the scope
closes, only the closing code itself (its finalizers, and what they start with a copy of their
context) may still resolve a sibling - and only one whose finalizer has not run yet. Anyone else
gets ScopeNotActiveError ("already closed"). The cases come from the 0.5.4 review (a, b, b2, d,
f, S1-S9); before, some of them got a disposed object or another request's live one, silently."""

import asyncio
import contextvars
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from py_autowired import Container

THREAD_INHERITS = bool(getattr(sys.flags, "thread_inherit_context", 0))


def outcome(resolve):
    try:
        made = resolve()
    except Exception as error:  # noqa: BLE001 - the test inspects it
        text = str(error)
        if "requires an active scope" in text:
            return "no-scope"
        if "already closed" in text:
            return "closed"
        return f"{type(error).__name__}: {text[:60]}"
    return f"{type(made).__name__}({'DISPOSED' if made.disposed else 'alive'})"


def build(hooks):
    container = Container()
    made = []
    for name in "PQR":
        def dispose(self, name=name):
            self.disposed = True
            hook = hooks.get(name)
            if hook:
                hook(self)

        async def aclose(self, name=name):
            self.disposed = True
            hook = hooks.get(name)
            if hook:
                result = hook(self)
                if asyncio.iscoroutine(result):
                    await result
        cls = type(name, (), {"disposed": False, "dispose": dispose, "aclose": aclose})
        container.register_scoped(cls)
        made.append(cls)
    return (container, *made)


def create_all(container, P, Q, R):
    container.resolve(P)
    container.resolve(Q)
    container.resolve(R)


def test_a_another_thread_with_an_earlier_copy_gets_no_object_while_the_scope_closes():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    with container.create_scope():
        create_all(container, P, Q, R)
        context = contextvars.copy_context()

        def in_r(_):
            thread = threading.Thread(
                target=lambda: out.__setitem__("v", context.run(lambda: outcome(lambda: container.resolve(Q)))))
            thread.start()
            thread.join()
        hooks["R"] = in_r
    assert out["v"] == "closed"


def test_b_a_finalizer_may_use_a_sibling_created_before_it():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    with container.create_scope():
        create_all(container, P, Q, R)
        hooks["R"] = lambda _: out.__setitem__("v", outcome(lambda: container.resolve(P)))
    assert out["v"] == "P(alive)"


def test_b2_a_finalizer_never_gets_a_sibling_that_is_already_disposed():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    with container.create_scope():
        create_all(container, P, Q, R)
        hooks["P"] = lambda _: out.__setitem__("v", outcome(lambda: container.resolve(R)))
    assert out["v"] == "closed", "a finalizer got a sibling that was already disposed"


def test_d_a_task_started_before_the_close_gets_no_object_while_it_closes():
    async def main():
        hooks, out = {}, {}
        container, P, Q, R = build(hooks)
        async with container.create_scope():
            create_all(container, P, Q, R)
            go = asyncio.Event()

            async def sibling():
                await go.wait()
                out["v"] = outcome(lambda: container.resolve(Q))
            task = asyncio.create_task(sibling())

            async def in_r(_):
                go.set()
                await task
            hooks["R"] = in_r
        return out["v"]
    assert asyncio.run(main()) == "closed"


def test_f_an_async_finalizer_may_use_a_sibling_not_finalized_yet():
    async def main():
        hooks, out = {}, {}
        container, P, Q, R = build(hooks)
        async with container.create_scope():
            create_all(container, P, Q, R)

            async def in_r(_):
                out["v"] = outcome(lambda: container.resolve(P))
            hooks["R"] = in_r
        return out["v"]
    assert asyncio.run(main()) == "P(alive)"


def test_s1_a_task_a_finalizer_started_gets_nothing_once_the_sibling_is_disposed():
    async def main():
        hooks, out, box = {}, {}, {}
        container, P, Q, R = build(hooks)
        go = asyncio.Event()
        async with container.create_scope():
            create_all(container, P, Q, R)

            async def background():
                await go.wait()
                out["v"] = outcome(lambda: container.resolve(Q))

            async def in_r(_):
                box["task"] = asyncio.create_task(background())

            async def in_p(_):
                go.set()
                await box["task"]
            hooks["R"] = in_r
            hooks["P"] = in_p
        return out["v"]
    assert asyncio.run(main()) == "closed", "a task started by a finalizer got a disposed sibling"


def test_s2_an_async_finalizer_may_pass_the_work_to_a_thread():
    async def main():
        hooks, out = {}, {}
        container, P, Q, R = build(hooks)
        async with container.create_scope():
            create_all(container, P, Q, R)

            async def in_r(_):
                out["v"] = await asyncio.to_thread(lambda: outcome(lambda: container.resolve(P)))
            hooks["R"] = in_r
        return out["v"]
    assert asyncio.run(main()) == "P(alive)"


def test_s3_fire_and_forget_work_of_a_finalizer_gets_nothing_once_the_sibling_is_disposed():
    hooks, out, box = {}, {}, {}
    container, P, Q, R = build(hooks)
    go = threading.Event()
    pool = ThreadPoolExecutor(1)
    with container.create_scope():
        create_all(container, P, Q, R)

        def work():
            go.wait()
            out["v"] = outcome(lambda: container.resolve(Q))

        def in_r(_):
            box["future"] = pool.submit(contextvars.copy_context().run, work)

        def in_p(_):
            go.set()
            box["future"].result()
        hooks["R"] = in_r
        hooks["P"] = in_p
    pool.shutdown()
    assert out["v"] == "closed", "fire-and-forget work of a finalizer got a disposed sibling"


def test_s4_a_finalizer_closing_its_own_inner_scope_still_uses_the_outer_siblings():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    with container.create_scope():
        create_all(container, P, Q, R)

        def in_r(_):
            hooks["R"] = None
            with container.create_scope():
                inner = outcome(lambda: container.resolve(P))
            out["v"] = (inner, outcome(lambda: container.resolve(P)))
        hooks["R"] = in_r
    assert out["v"] == ("P(alive)", "P(alive)")


def test_s5_a_second_closer_is_not_the_one_closing():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    with container.create_scope() as scope:
        create_all(container, P, Q, R)
        context = contextvars.copy_context()

        def second():
            scope.state.close()
            out["v"] = outcome(lambda: container.resolve(P))

        def in_r(_):
            thread = threading.Thread(target=lambda: context.run(second))
            thread.start()
            thread.join()
        hooks["R"] = in_r
    assert out["v"] == "closed"


def test_s6_a_plain_thread_of_a_finalizer_gets_nothing_once_the_sibling_is_disposed():
    hooks, out, box = {}, {}, {}
    container, P, Q, R = build(hooks)
    go = threading.Event()
    with container.create_scope():
        create_all(container, P, Q, R)

        def work():
            go.wait()
            out["v"] = outcome(lambda: container.resolve(Q))

        def in_r(_):
            box["thread"] = threading.Thread(target=work)
            box["thread"].start()

        def in_p(_):
            go.set()
            box["thread"].join()
        hooks["R"] = in_r
        hooks["P"] = in_p
    # a plain thread does not see the request's scope (3.11 never; 3.14t: its birth drops the
    # library's context values) - never a disposed object
    assert out["v"] == "no-scope"


def test_s6m_a_plain_thread_of_a_finalizer_does_not_see_the_scope():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    with container.create_scope():
        create_all(container, P, Q, R)

        def in_r(_):
            thread = threading.Thread(target=lambda: out.__setitem__("v", outcome(lambda: container.resolve(P))))
            thread.start()
            thread.join()
        hooks["R"] = in_r
    assert out["v"] == "no-scope"  # as on 3.11; pass the context explicitly to share it


def test_s7_a_pool_worker_born_while_a_scope_closes_never_serves_another_request_from_it():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    pool = ThreadPoolExecutor(1)
    with container.create_scope():
        create_all(container, P, Q, R)

        def other_request():
            out["v"] = pool.submit(lambda: outcome(lambda: container.resolve(Q))).result()

        def in_r(_):
            pool.submit(lambda: None).result()  # the worker is born here
            thread = threading.Thread(target=lambda: contextvars.Context().run(other_request))
            thread.start()
            thread.join()
        hooks["R"] = in_r
    pool.shutdown()
    assert out["v"] == "no-scope", "another request got an object of the closing scope"


def test_s7b_the_same_with_asyncio_run_inside_the_work():
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)
    pool = ThreadPoolExecutor(1)
    with container.create_scope():
        create_all(container, P, Q, R)

        async def coroutine():
            return outcome(lambda: container.resolve(Q))

        def other_request():
            out["v"] = pool.submit(lambda: asyncio.run(coroutine())).result()

        def in_r(_):
            pool.submit(lambda: None).result()
            thread = threading.Thread(target=lambda: contextvars.Context().run(other_request))
            thread.start()
            thread.join()
        hooks["R"] = in_r
    pool.shutdown()
    assert out["v"] == "no-scope"


def test_s8_two_scopes_of_one_container_closing_at_once_do_not_mix():
    hooks = {}
    container, P, Q, R = build(hooks)
    barrier = threading.Barrier(2, timeout=10)
    out, per_thread = {}, {}
    hooks["R"] = lambda self: per_thread[threading.current_thread().name](self)  # each request its own

    def request():
        with container.create_scope():
            create_all(container, P, Q, R)
            mine = container.resolve(P)

            def in_r(_):
                barrier.wait()
                got = container.resolve(P)
                out[threading.current_thread().name] = (outcome(lambda: got), got is mine)
                barrier.wait()
            per_thread[threading.current_thread().name] = in_r

    threads = [threading.Thread(target=request, name=name) for name in ("1", "2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert out == {"1": ("P(alive)", True), "2": ("P(alive)", True)}


def test_s9_a_failing_finalizer_leaves_nothing_behind():
    hooks = {}
    container, P, Q, R = build(hooks)

    def in_r(_):
        raise RuntimeError("on purpose")
    hooks["R"] = in_r
    with pytest.raises(RuntimeError, match="on purpose"):
        with container.create_scope():
            create_all(container, P, Q, R)
    # the closing code's exemption ended with the close: nothing is handed out afterwards
    assert outcome(lambda: container.resolve(P)) == "no-scope"
    from py_autowired import container as container_module
    closing = getattr(container_module, "_CLOSING", None)
    assert closing is None or closing.get() is None, "the closing mark leaked out of the close"


def test_all_objects_are_finalized_once_and_in_reverse_order():
    hooks, order = {}, []
    container, P, Q, R = build(hooks)
    for name in "PQR":
        hooks[name] = lambda self, name=name: order.append(name)
    with container.create_scope():
        create_all(container, P, Q, R)
    assert order == ["R", "Q", "P"]


# ---- review round 3: only objects that are finalized leave the scope before their finalizer ----

def _resource_then_settings():
    container = Container()
    out = {}

    class Settings:  # no finalizer: never disposed, stays usable while the scope closes
        pass

    class Resource:
        def close(self):
            out["v"] = outcome_plain(lambda: container.resolve(Settings))

        async def aclose(self):
            self.close()
    container.register_scoped(Resource)
    container.register_scoped(Settings)
    return container, Resource, Settings, out


def outcome_plain(resolve):
    try:
        return type(resolve()).__name__
    except Exception as error:  # noqa: BLE001
        return f"{type(error).__name__}: {str(error)[:40]}"


def test_a_finalizer_may_use_a_later_sibling_that_has_no_finalizer():
    container, Resource, Settings, out = _resource_then_settings()
    with container.create_scope():
        container.resolve(Resource)
        container.resolve(Settings)  # created after Resource: closed before it
    assert out["v"] == "Settings", "a sibling without a finalizer was taken away before the close finished"


def test_an_async_finalizer_may_use_a_later_sibling_that_has_no_finalizer():
    async def main():
        container, Resource, Settings, out = _resource_then_settings()
        async with container.create_scope():
            container.resolve(Resource)
            container.resolve(Settings)
        return out["v"]
    assert asyncio.run(main()) == "Settings"


# ---- review round 4: a finalizer that cannot even be read does not stop the close ----

def _broken_then_normal():
    container = Container()
    closed = []

    class Normal:
        def close(self):
            closed.append("Normal")

        async def aclose(self):
            closed.append("Normal")

    class Broken:  # reading its finalizer raises
        @property
        def close(self):
            raise RuntimeError("finalizer cannot be read")

        @property
        def aclose(self):
            raise RuntimeError("finalizer cannot be read")
    container.register_scoped(Normal)
    container.register_scoped(Broken)
    return container, Normal, Broken, closed


def test_a_finalizer_that_cannot_be_read_does_not_stop_the_close():
    container, Normal, Broken, closed = _broken_then_normal()
    scope = container.create_scope()
    error = None
    try:
        with scope:
            container.resolve(Normal)
            container.resolve(Broken)  # closed first, and fails
    except RuntimeError as caught:
        error = caught
    assert error is not None and "cannot be read" in str(error)
    assert closed == ["Normal"], "the other finalizers did not run"
    assert scope.state._closing == {} and scope.state.creation_order == [], "the close did not finish"


def test_an_async_finalizer_that_cannot_be_read_does_not_stop_the_close():
    async def main():
        container, Normal, Broken, closed = _broken_then_normal()
        scope = container.create_scope()
        error = None
        try:
            async with scope:
                container.resolve(Normal)
                container.resolve(Broken)
        except RuntimeError as caught:
            error = caught
        return error, closed, scope.state._closing, scope.state.creation_order
    error, closed, left, order = asyncio.run(main())
    assert error is not None and "cannot be read" in str(error)
    assert closed == ["Normal"] and left == {} and order == []


# ---- review round 5: a finalizer is read once, and a sync close reads only the sync names ----

def test_a_sync_close_calls_dispose_even_if_reading_aclose_would_fail():
    container = Container()
    closed = []

    class Both:
        def dispose(self):
            closed.append("dispose")

        @property
        def aclose(self):  # irrelevant to a sync close; reading it fails
            raise RuntimeError("aclose cannot be read")
    container.register_scoped(Both)
    error = None
    try:
        with container.create_scope():
            container.resolve(Both)
    except Exception as caught:  # noqa: BLE001 - the test inspects it
        error = caught
    assert closed == ["dispose"] and error is None, f"the sync finalizer was not called ({error!r})"


@pytest.mark.parametrize("closing", ["sync", "async"])
def test_the_finalizer_read_first_is_the_one_called(closing):
    container = Container()
    closed = []

    async def closer():
        await asyncio.sleep(0)
        closed.append("aclose")

    class Once:
        """Its finalizer can be read once: a second read finds nothing."""
        def __init__(self):
            self.reads = 0

        @property
        def close(self):
            self.reads += 1
            return (lambda: closed.append("close")) if self.reads == 1 else None

        @property
        def aclose(self):
            self.reads += 1
            return closer if self.reads == 1 else None
    container.register_scoped(Once)
    if closing == "sync":
        with container.create_scope():
            made = container.resolve(Once)
        expected = ["close"]
    else:
        async def main():
            async with container.create_scope():
                return container.resolve(Once)
        made = asyncio.run(main())
        expected = ["aclose"]
    assert closed == expected, "the finalizer was read twice and never called"
    assert made.reads == 1



# ---- a3a2 consensus (Z): the close waits for a creation in flight; meanwhile nothing is handed out ----

def _close_while_a_creation_is_in_flight(container, Q, during_wait, in_finalizer=None, hooks=None):
    """Q is ready; another thread creates Slow (its gate held); a third thread closes the scope - the
    close flags it closed and waits for that gate. ``during_wait`` runs while the close waits;
    ``in_finalizer`` runs inside Q's finalizer (after the wait). Returns when everything is done."""
    import time

    started, finish = threading.Event(), threading.Event()

    class Slow:
        def __init__(self):
            started.set()
            finish.wait(10)

    container.register_scoped(Slow)
    scope = container.create_scope()
    scope.__enter__()
    try:
        container.resolve(Q)
        context = contextvars.copy_context()  # the readers' (a context runs in one thread at a time)
        if in_finalizer is not None:
            hooks["Q"] = lambda _: in_finalizer(scope, context)
        creator = threading.Thread(target=contextvars.copy_context().run, args=(container.resolve, Slow))
        creator.start()
        assert started.wait(10)
        closer = threading.Thread(target=scope.state.close)
        closer.start()
        deadline = time.monotonic() + 10
        while not scope.state.closed and time.monotonic() < deadline:
            time.sleep(0.005)
        assert scope.state.closed and closer.is_alive(), "the close did not wait for the creation"
        during_wait(scope, context)
        finish.set()
        creator.join(10)
        closer.join(10)
        assert not closer.is_alive()
    finally:
        finish.set()
        scope._leave()


def test_e_a_ready_object_is_never_handed_out_while_the_close_waits_for_a_creation():
    """Z (a3a2 consensus, a real defect): after `closed` was set and while the close waited for a
    creation in flight, the lock-free path still handed a ready object - about to be disposed - to
    another context."""
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)

    def read(scope, context):
        reader = threading.Thread(
            target=lambda: out.__setitem__("v", context.run(lambda: outcome(lambda: container.resolve(Q)))))
        reader.start()
        reader.join(10)

    _close_while_a_creation_is_in_flight(container, Q, read)
    assert out["v"] == "closed", out


def test_f_after_the_wait_the_objects_are_out_of_the_lock_free_path():
    """U1c (gate): the swap after waiting for the creations empties the lock-free path - a finalizer
    sees no object there, and a reader with an earlier copy of the context gets none."""
    hooks, out = {}, {}
    container, P, Q, R = build(hooks)

    def in_finalizer(scope, context):
        out["instances"] = dict(scope.state.instances)
        reader = threading.Thread(
            target=lambda: out.__setitem__("v", context.run(lambda: outcome(lambda: container.resolve(P)))))
        reader.start()
        reader.join(10)

    _close_while_a_creation_is_in_flight(container, Q, lambda scope, context: None, in_finalizer, hooks)
    assert out["instances"] == {}, out
    assert out["v"] == "closed", out


# ---- a3a2 consensus: the creation gates ----

def test_g_a_failed_scoped_creation_leaves_no_gate():
    """N5d (gate): a scoped factory that fails leaves no creation gate behind - one per failed slot
    would pile up in a long-lived scope."""
    from py_autowired import LifetimeScope

    container = Container()
    A = type("A", (), {})
    B = type("B", (), {})

    def boom(_):
        raise KeyError("boom")

    container.register_factory(A, boom, LifetimeScope.SCOPED)
    container.register_factory(B, boom, LifetimeScope.SCOPED)
    with container.create_scope() as scope:
        for cls in (A, B, A):
            with pytest.raises(KeyError):
                container.resolve(cls)
            assert scope.state.gates == {}, scope.state.gates


def test_h_an_inner_resolution_of_the_same_slot_keeps_the_outer_gate():
    """N5e (gate, Y and Z): a context copied before the resolution started lets the outer factory
    resolve the same slot again (no cycle is seen there): the gate is taken a second time by the same
    thread. The inner resolution must not remove the gate the outer one still holds."""
    from py_autowired import LifetimeScope

    container = Container()
    S = type("S", (), {})
    seen = {}
    with container.create_scope() as scope:
        early = contextvars.copy_context()

        def outer(c):
            if "outer" in seen:
                return S()  # the inner call
            seen["outer"] = True
            gate = scope.state.gates.get(S)
            early.run(c.resolve, S)
            seen["kept"] = scope.state.gates.get(S) is gate
            return S()

        container.register_factory(S, outer, LifetimeScope.SCOPED)
        container.resolve(S)
    assert seen.get("kept") is True, seen
