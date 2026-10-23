"""Optional FastAPI bridge for runtime field injection.

Only this module imports FastAPI. Install ``py-autowired[fastapi]`` to use it.
Class patches and the active container remain process-wide; attach one
container per process. Request scopes are isolated through context variables.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import sys
from typing import Any, Callable, Coroutine, Iterable, TypeVar

from anyio import CancelScope
from fastapi import FastAPI, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .autowired import (
    _check_active, _find_project_root, _inject_unpatched, _warn_unfilled_registered,
    activate_container, auto_inject,
)
from . import autowired
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
        _check_active(self.container)  # before anything of this request is built
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
    close_on_shutdown: bool = True,
) -> FastAPI:
    """Install startup autowiring and a fresh async scope for every HTTP request.

    Call before starting the application, after service registrations. The
    existing application lifespan is preserved and runs inside activation.
    ``root_dir``, ``exclude_dirs``, and ``validate`` are forwarded to
    :func:`auto_inject`. Opt-in validation runs during lifespan startup.
    All middleware and exception handlers, including Exception/500 handlers,
    run inside the request scope.

    Without ``root_dir`` the project root is searched upwards from the file
    that calls ``attach_container``, so ``uvicorn main:app`` and
    ``python main.py`` find the same project. With ``close_on_shutdown`` (the
    default) container-created singletons are disposed when the application
    stops; a later startup creates fresh ones. Supplied instances
    (``register_instance``) are never disposed.
    """
    if root_dir is None:
        caller = sys._getframe(1).f_globals.get("__file__")
        if caller:
            root_dir = str(_find_project_root(Path(caller).resolve().parent))
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
        try:
            async with previous_lifespan(application) as state:
                yield state
        finally:
            if close_on_shutdown:
                with CancelScope(shield=True):
                    await container._arelease_singletons()

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
    field injection. An unregistered class discovery never patched (outside the
    project root, or created after startup) is not changed: the None fields of
    the object built here are filled after its ``__init__`` returns, so
    ``__init__`` itself cannot use them yet. A REGISTERED type whose class was
    never patched is not filled from here (a shared singleton would keep the
    first request's objects): each None field stays ``None`` with a WARNING
    naming it - move the class into the project root or use constructor
    injection. If another application's container is the active one, it
    raises ``DependencyInjectionError`` before building anything (one container
    per process; a best-effort catch, not a guarantee). With no active container
    at all (startup has not run, e.g. a ``TestClient`` used without ``with``) the
    fields are not filled, as in 0.5.3: each one stays ``None`` with a WARNING
    naming it. Dependencies are async so resolution stays in the request context
    even when the endpoint itself is synchronous.
    """
    # The class last found patched, per branch: its patched __init__ handles its fields, and a
    # patched class stays patched - the checks below run again only for another class.
    registered_patched = built_patched = None

    async def dependency(request: Request) -> T:
        nonlocal registered_patched, built_patched
        container: Container = request.app.state.py_autowired_container
        active = autowired._NAME_MAP_CACHE  # _check_active's own test, inline; it raises
        if active is not None and active._container is not container:
            _check_active(container)  # before resolving or building anything
        if container.is_registered(service_type):
            instance = container.resolve(service_type)
            cls = type(instance)
            if cls is not registered_patched and _warn_unfilled_registered(instance):
                registered_patched = cls  # never filled from here: said, not silent
            return instance
        instance = service_type()
        cls = type(instance)
        if cls is not built_patched and _inject_unpatched(instance, container):
            built_patched = cls
        return instance

    return dependency
