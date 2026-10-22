"""Shared pieces of the Flask and Django bridges.

Imports no web framework. Each request runs inside its own container scope;
startup (discovery + activation) happens once, on the first request, when every
application module has been imported.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from .autowired import _find_project_root, activate_container, auto_inject
from .container import Container


def caller_root(depth: int, marker: Optional[str] = None) -> Optional[str]:
    """Project root seen from the file that called the public bridge function.

    ``depth`` counts frames above this function's caller. A ``marker`` file
    (Django's ``manage.py``) wins when one is found walking upwards.
    """
    caller = sys._getframe(depth + 1).f_globals.get("__file__")
    if not caller:
        return None
    start = Path(caller).resolve().parent
    if marker is not None:
        for parent in (start, *start.parents):
            if (parent / marker).is_file():
                return str(parent)
    return str(_find_project_root(start))


class Startup:
    """Runs auto_inject and activation exactly once."""

    def __init__(
        self,
        container: Container,
        root_dir: Optional[str],
        exclude_dirs: Optional[Iterable[str]],
        validate: bool,
    ) -> None:
        self.container = container
        self.root_dir = root_dir
        self.exclude_dirs = exclude_dirs
        self.validate = validate
        self.done = False
        self._lock = threading.Lock()

    def ensure(self) -> None:
        if self.done:  # every request after the first: no lock
            return
        with self._lock:
            if self.done:
                return
            auto_inject(
                self.container,
                root_dir=self.root_dir,
                exclude_dirs=self.exclude_dirs,
                validate=self.validate,
            )
            activate_container(self.container)
            self.done = True


class _ClosingResult:
    """WSGI result that ends the request scope when the server closes it."""

    def __init__(self, result: Iterable[bytes], scope: Any) -> None:
        self._result = result
        self._scope = scope

    def __iter__(self):
        return iter(self._result)

    def close(self) -> None:
        try:
            close = getattr(self._result, "close", None)
            if close is not None:
                close()
        finally:
            self._scope.__exit__(None, None, None)


class WsgiRequestScope:
    """WSGI middleware: one container scope per request, kept open while the
    response body is produced (streaming) and closed with the response."""

    def __init__(self, app: Any, container: Container, startup: Startup) -> None:
        self.app = app
        self.container = container
        self.startup = startup

    def __call__(self, environ: dict, start_response: Any) -> Iterable[bytes]:
        self.startup.ensure()
        scope = self.container.create_scope()
        scope.__enter__()
        try:
            result = self.app(environ, start_response)
        except BaseException:
            scope.__exit__(*sys.exc_info())
            raise
        return _ClosingResult(result, scope)


class AsgiRequestScope:
    """ASGI middleware with the same per-request scope (Django under ASGI)."""

    def __init__(self, app: Any, container: Container, startup: Startup) -> None:
        self.app = app
        self.container = container
        self.startup = startup

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.startup.ensure()
        request_scope = self.container.create_scope()
        await request_scope.__aenter__()
        try:
            await self.app(scope, receive, send)
        finally:
            try:
                closing = request_scope.state.aclose()
                try:
                    asyncio.get_running_loop()
                except RuntimeError:  # not asyncio (e.g. trio): no shield available here
                    await closing
                else:
                    # A cancelled request must still close its scoped services, and
                    # their errors must reach the caller: wait for the shielded
                    # closing through every cancellation, then report. Only the
                    # closing runs in the task; the scope is left in this context.
                    task = asyncio.ensure_future(closing)
                    cancelled = None
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError as error:
                            cancelled = error
                    task.result()  # a cleanup error wins over the cancellation
                    if cancelled is not None:
                        raise cancelled
            finally:
                request_scope._leave()


def is_asgi(application: Any) -> bool:
    return inspect.iscoroutinefunction(getattr(application, "__call__", None))
