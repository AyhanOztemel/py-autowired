"""Optional Flask bridge for runtime field injection.

This module does not import Flask itself; it only wraps ``app.wsgi_app``.
"""

from __future__ import annotations

import atexit
from typing import Any, Iterable, Optional

from ._web import Startup, WsgiRequestScope, caller_root
from .container import Container

__all__ = ["attach_container"]


def attach_container(
    app: Any,
    container: Container,
    *,
    root_dir: Optional[str] = None,
    exclude_dirs: Optional[Iterable[str]] = None,
    validate: bool = False,
    close_on_shutdown: bool = True,
) -> Any:
    """Connect a container to a Flask application with one call.

    - Every request runs in its own scope; scoped services are shared within a
      request and closed when the response is finished.
    - Discovery and activation run once, on the first request, after all
      application modules have been imported.
    - Without ``root_dir`` the project root is searched upwards from the file
      that calls ``attach_container``.
    - With ``close_on_shutdown`` (default) container-created singletons are
      disposed when the process exits. Supplied instances are never disposed.
    """
    extensions = app.extensions
    if "py_autowired" in extensions:
        raise ValueError("A container is already attached to this application")
    if root_dir is None:
        root_dir = caller_root(1)
    startup = Startup(container, root_dir, exclude_dirs, validate)
    app.wsgi_app = WsgiRequestScope(app.wsgi_app, container, startup)
    extensions["py_autowired"] = container
    if close_on_shutdown:
        atexit.register(container._release_singletons)
    return app
