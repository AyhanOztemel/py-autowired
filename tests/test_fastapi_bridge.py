"""Real HTTP coverage for the optional bridge and its automatic field graph."""

import asyncio
from contextlib import asynccontextmanager
import importlib
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
import anyio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask

from py_autowired import Container, autowired
from py_autowired.fastapi import attach_container, injected


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    # Exercise real startup discovery, not private class-patching helpers.
    module_name = "bridge_product_graph"
    (tmp_path / f"{module_name}.py").write_text(
        "created = []\n"
        "events = []\n"
        "class ProductRepository:\n"
        "    def __init__(self):\n"
        "        self.closed = False\n"
        "        created.append(self)\n"
        "    def close(self):\n"
        "        self.closed = True\n"
        "        events.append('closed')\n"
        "class ProductService:\n"
        "    def __init__(self):\n"
        "        self.product_repository_instance = None\n"
        "class ProductController:\n"
        "    product_service_instance = None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    module = importlib.import_module(module_name)
    # Restore the process-wide graph after every test.
    monkeypatch.setattr(autowired, "_NAME_MAP_CACHE", autowired._NAME_MAP_CACHE)
    container = Container()
    container.register_scoped(module.ProductRepository)
    container.register_transient(module.ProductService)
    events = []

    @asynccontextmanager
    async def lifespan(app):
        # Startup must already have installed field patches and activation.
        assert module.ProductController.__di_attrs__
        assert autowired._NAME_MAP_CACHE._container is container
        events.append("startup")
        yield {"startup_marker": "ready"}
        events.append("shutdown")

    app = FastAPI(lifespan=lifespan)
    attach_container(app, container, root_dir=str(tmp_path))
    yield SimpleNamespace(
        app=app, container=container, module=module, events=events
    )
    sys.modules.pop(module_name, None)


@pytest.mark.parametrize("async_endpoint", [False, True])
def test_request_sharing_isolation_and_existing_lifespan(bridge, async_endpoint):
    controller = bridge.module.ProductController

    def inspect_graph(
        first=Depends(injected(controller), use_cache=False),
        second=Depends(injected(controller), use_cache=False),
    ):
        first_service = first.product_service_instance
        second_service = second.product_service_instance
        repository = first_service.product_repository_instance
        assert first is not second
        assert first_service is not second_service
        assert repository is second_service.product_repository_instance
        assert not repository.closed
        assert bridge.container.current_scope is not None
        # Ordinary runtime construction also shares this request's scope.
        assert (
            controller().product_service_instance.product_repository_instance
            is repository
        )
        return {"repository": id(repository)}

    if async_endpoint:
        async def endpoint(
            first=Depends(injected(controller), use_cache=False),
            second=Depends(injected(controller), use_cache=False),
        ):
            await asyncio.sleep(0)
            return inspect_graph(first, second)
    else:
        endpoint = inspect_graph
    bridge.app.get("/products")(endpoint)

    with TestClient(bridge.app) as client:
        assert bridge.events == ["startup"]
        first = client.get("/products")
        second = client.get("/products")
        assert first.status_code == second.status_code == 200
        assert first.json()["repository"] != second.json()["repository"]
        assert len(bridge.module.created) == 2
        assert all(item.closed for item in bridge.module.created)
    assert bridge.events == ["startup", "shutdown"]
    assert bridge.container.current_scope is None


def test_concurrent_requests_keep_their_scope_across_awaits(bridge):
    controller = bridge.module.ProductController
    entered = []
    scopes = []
    barrier = None
    total = 8

    @bridge.app.get("/products")
    async def endpoint(current=Depends(injected(controller))):
        nonlocal barrier
        if barrier is None:
            barrier = asyncio.Event()
        repository = current.product_service_instance.product_repository_instance
        scope = bridge.container.current_scope
        entered.append(repository)
        scopes.append(scope)
        if len(entered) == total:
            barrier.set()
        await asyncio.wait_for(barrier.wait(), timeout=10)
        assert bridge.container.current_scope is scope
        assert not repository.closed
        assert (
            controller().product_service_instance.product_repository_instance
            is repository
        )
        return {"repository": id(repository)}

    with TestClient(bridge.app) as client:
        async def scenario():
            return await asyncio.gather(
                *(asyncio.to_thread(client.get, "/products") for _ in range(total))
            )
        responses = asyncio.run(scenario())
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["repository"] for response in responses}) == total
    assert len({id(scope) for scope in scopes}) == total
    assert all(scope.closed for scope in scopes)
    assert all(repository.closed for repository in entered)


@pytest.mark.parametrize(
    ("finalizer_name", "is_async"),
    [("close", False), ("dispose", False), ("close", True),
     ("dispose", True), ("aclose", True)],
)
def test_scope_awaits_all_supported_finalizers(bridge, finalizer_name, is_async):
    repository_type = bridge.module.ProductRepository
    del repository_type.close
    completed = []

    if is_async:
        async def finalize(self):
            await asyncio.sleep(0)
            self.closed = True
            completed.append(self)
    else:
        def finalize(self):
            self.closed = True
            completed.append(self)
    setattr(repository_type, finalizer_name, finalize)

    @bridge.app.get("/products")
    def endpoint(current=Depends(injected(bridge.module.ProductController))):
        assert not current.product_service_instance.product_repository_instance.closed
        return {"ok": True}

    with TestClient(bridge.app) as client:
        assert client.get("/products").status_code == 200
        assert completed == bridge.module.created
        assert all(item.closed for item in completed)


def test_scope_closes_when_an_endpoint_raises(bridge):
    @bridge.app.get("/products")
    async def endpoint(current=Depends(injected(bridge.module.ProductController))):
        assert current.product_service_instance.product_repository_instance
        raise RuntimeError("request failed")

    with TestClient(bridge.app) as client:
        with pytest.raises(RuntimeError, match="request failed"):
            client.get("/products")
        assert bridge.module.created[0].closed


def test_async_resource_closes_under_active_cancellation(bridge):
    repository_type = bridge.module.ProductRepository
    del repository_type.close
    completed = []
    scopes = []

    async def aclose(self):
        await anyio.sleep(0)
        self.closed = True
        completed.append(self)

    repository_type.aclose = aclose

    @bridge.app.get("/cancel")
    async def endpoint():
        current = bridge.module.ProductController()
        assert not current.product_service_instance.product_repository_instance.closed
        scopes.append(bridge.container.current_scope)
        await anyio.sleep_forever()

    class CancelRequestMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            with anyio.fail_after(0.05):
                await self.app(scope, receive, send)

    # Cancel outside the entire app so cancellation is active during cleanup.
    with TestClient(CancelRequestMiddleware(bridge.app)) as client:
        with pytest.raises(TimeoutError):
            client.get("/cancel")
        assert len(completed) == 1
        assert completed == bridge.module.created
        assert completed[0].closed
        assert scopes[0].closed


@pytest.mark.parametrize("exception_kind", ["http", "custom"])
def test_specific_exception_handlers_share_the_open_request_scope(bridge, exception_kind):
    class ProductFailure(Exception):
        pass

    exception_type = HTTPException if exception_kind == "http" else ProductFailure
    observed = []

    @bridge.app.exception_handler(exception_type)
    async def handle_failure(request, exc):
        current = bridge.module.ProductController()
        repository = current.product_service_instance.product_repository_instance
        assert repository is observed[0]
        assert bridge.container.current_scope is observed[1]
        assert not repository.closed
        return JSONResponse({"handled": exception_kind}, status_code=409)

    @bridge.app.get("/handled")
    async def endpoint():
        current = bridge.module.ProductController()
        observed.extend([
            current.product_service_instance.product_repository_instance,
            bridge.container.current_scope,
        ])
        if exception_kind == "http":
            raise HTTPException(status_code=409, detail="product failed")
        raise ProductFailure("product failed")

    with TestClient(bridge.app) as client:
        response = client.get("/handled")
        assert response.status_code == 409
        assert response.json() == {"handled": exception_kind}
        assert observed[0].closed
        assert observed[1].closed


@pytest.mark.parametrize("handler_key", [Exception, 500])
def test_generic_exception_handler_shares_the_open_request_scope(bridge, handler_key):
    observed = []
    completed = []

    @bridge.app.exception_handler(handler_key)
    async def handle_failure(request, exc):
        current = bridge.module.ProductController()
        repository = current.product_service_instance.product_repository_instance
        assert repository is observed[0]
        assert bridge.container.current_scope is observed[1]
        assert not repository.closed

        async def background():
            await asyncio.sleep(0)
            assert bridge.container.current_scope is observed[1]
            assert not repository.closed
            completed.append("response finished")

        return JSONResponse(
            {"handled": "inside scope"}, status_code=500,
            background=BackgroundTask(background),
        )

    @bridge.app.get("/unhandled")
    async def endpoint():
        current = bridge.module.ProductController()
        observed.extend([
            current.product_service_instance.product_repository_instance,
            bridge.container.current_scope,
        ])
        raise RuntimeError("product failed")

    with TestClient(bridge.app, raise_server_exceptions=False) as client:
        response = client.get("/unhandled")
        assert response.status_code == 500
        assert response.json() == {"handled": "inside scope"}
        assert completed == ["response finished"]
        assert bridge.module.created == [observed[0]]
        assert observed[0].closed
        assert observed[1].closed


def test_user_middleware_shares_one_scope_even_after_stack_rebuild(bridge, monkeypatch):
    scopes = []
    scope_count = 0
    create_scope = bridge.container.create_scope

    def tracked_create_scope():
        nonlocal scope_count
        scope_count += 1
        return create_scope()

    monkeypatch.setattr(bridge.container, "create_scope", tracked_create_scope)

    class InspectScopeMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)
            request_scope = bridge.container.current_scope
            assert request_scope is not None
            scopes.append(request_scope)
            current = bridge.module.ProductController()
            repository = current.product_service_instance.product_repository_instance
            assert not repository.closed
            bridge.module.events.append("middleware entered")
            await self.app(scope, receive, send)
            assert bridge.container.current_scope is request_scope
            assert not repository.closed
            bridge.module.events.append("middleware exited")

    # Middleware registered after attachment must also stay inside the scope.
    bridge.app.add_middleware(InspectScopeMiddleware)

    @bridge.app.get("/products")
    async def endpoint(current=Depends(injected(bridge.module.ProductController))):
        repository = current.product_service_instance.product_repository_instance
        assert repository is bridge.module.created[-1]
        assert bridge.container.current_scope is scopes[-1]
        bridge.module.events.append("endpoint")
        return {"ok": True}

    with TestClient(bridge.app) as client:
        for _ in range(2):
            bridge.app.middleware_stack = bridge.app.build_middleware_stack()
            assert client.get("/products").status_code == 200
        assert scope_count == len(scopes) == len(bridge.module.created) == 2
        assert scopes[0] is not scopes[1]
        assert all(scope.closed for scope in scopes)
        assert bridge.module.events == [
            "middleware entered", "endpoint", "middleware exited", "closed",
        ] * 2


def test_stream_and_background_task_finish_before_scope_disposal(bridge):
    observed = []

    @bridge.app.get("/products")
    async def endpoint(current=Depends(injected(bridge.module.ProductController))):
        repository = current.product_service_instance.product_repository_instance

        async def stream():
            await asyncio.sleep(0)
            assert not repository.closed
            observed.append("stream")
            yield "products"

        async def background():
            await asyncio.sleep(0)
            assert not repository.closed
            observed.append("background")

        return StreamingResponse(stream(), background=BackgroundTask(background))

    with TestClient(bridge.app) as client:
        assert client.get("/products").text == "products"
        assert observed == ["stream", "background"]
        assert bridge.module.created[0].closed


def test_registered_dependency_keeps_its_scoped_lifetime(bridge):
    controller = bridge.module.ProductController
    bridge.container.register_scoped(controller)
    retained = []

    @bridge.app.get("/products")
    async def endpoint(
        first=Depends(injected(controller), use_cache=False),
        second=Depends(injected(controller), use_cache=False),
    ):
        assert first is second
        retained.append(first)
        return {"controller": id(first)}

    with TestClient(bridge.app) as client:
        # Keep the first object alive so the identity cannot be recycled.
        first = client.get("/products")
        second = client.get("/products")
        assert first.json()["controller"] != second.json()["controller"]
        assert len(retained) == 2


def test_existing_lifespan_state_reaches_requests(bridge):
    @bridge.app.get("/state")
    async def endpoint(request: Request):
        assert bridge.container.current_scope is not None
        return {"marker": request.state.startup_marker}

    with TestClient(bridge.app) as client:
        assert client.get("/state").json() == {"marker": "ready"}


def test_a_second_attachment_fails_explicitly(bridge):
    with pytest.raises(ValueError, match="already attached"):
        attach_container(bridge.app, bridge.container)
