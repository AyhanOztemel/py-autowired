"""Optional FastAPI bridge for runtime field injection.

Only this module imports FastAPI. Install ``py-autowired[fastapi]`` to use it.
Class patches and the active container remain process-wide; attach one
container per process. Request scopes are isolated through context variables.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import sys
from typing import Any, Callable, Coroutine, Iterable, TypeVar

from anyio import CancelScope
from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .autowired import activate_container, auto_inject
from .container import Container


T = TypeVar("T")
__all__ = ["attach_container", "injected"]


class _RequestScopeMiddleware:
    def __init__(self, app: ASGIApp, container: Container) -> None:
        self.app = app
        self.container = container

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Pure ASGI keeps the scope alive through streaming and background work,
        # and awaits both synchronous and asynchronous resource finalizers.
        request_scope = self.container.create_scope()
        await request_scope.__aenter__()
        try:
            await self.app(scope, receive, send)
        finally:
            # AnyIO cancellation stays active while unwinding a request. Shield
            # finalizers so their await points can still release resources.
            with CancelScope(shield=True):
                await request_scope.__aexit__(*sys.exc_info())


def attach_container(
    app: FastAPI,
    container: Container,
    *,
    root_dir: str | None = None,
    exclude_dirs: Iterable[str] | None = None,
    validate: bool = False,
) -> FastAPI:
    """Install startup autowiring and a fresh async scope for every HTTP request.

    Call before starting the application, after service registrations. The
    existing application lifespan is preserved and runs inside activation.
    ``root_dir``, ``exclude_dirs``, and ``validate`` are forwarded to
    :func:`auto_inject`. Opt-in validation runs during lifespan startup.
    All middleware and exception handlers, including Exception/500 handlers,
    run inside the request scope.
    """
    if hasattr(app.state, "py_autowired_container"):
        raise ValueError("A container is already attached to this application")
    if app.middleware_stack is not None:
        raise RuntimeError("Cannot add middleware after an application has started")
    previous_lifespan = app.router.lifespan_context
    previous_build_middleware_stack = app.build_middleware_stack

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        auto_inject(container, root_dir=root_dir, exclude_dirs=exclude_dirs, validate=validate)
        activate_container(container)
        async with previous_lifespan(application) as state:
            yield state

    def build_middleware_stack() -> ASGIApp:
        # Wrap the complete stack, including Starlette's ServerErrorMiddleware.
        # Build afresh each time so rebuilding never nests request scopes.
        return _RequestScopeMiddleware(previous_build_middleware_stack(), container)

    app.build_middleware_stack = build_middleware_stack
    app.state.py_autowired_container = container
    app.router.lifespan_context = lifespan
    return app


def injected(service_type: type[T]) -> Callable[[Request], Coroutine[Any, Any, T]]:
    """Build a ``Depends`` dependency whose fields are filled by auto-injection.

    Registered types retain their configured lifetime. Unregistered controller
    classes are constructed without arguments and receive the same automatic
    field injection. Dependencies are async so resolution stays in the request
    context even when the endpoint itself is synchronous.
    """
    async def dependency(request: Request) -> T:
        container: Container = request.app.state.py_autowired_container
        if container.is_registered(service_type):
            return container.resolve(service_type)
        return service_type()

    return dependency
