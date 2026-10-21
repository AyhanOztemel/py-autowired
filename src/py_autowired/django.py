"""Optional Django bridge for runtime field injection.

This module does not import Django itself; it wraps the WSGI or ASGI
application object, so ``settings.py`` needs no change.
"""

from __future__ import annotations

import atexit
from typing import Any, Iterable, Optional

from ._web import AsgiRequestScope, Startup, WsgiRequestScope, caller_root, is_asgi
from .container import Container

__all__ = ["attach_container"]


def attach_container(
    application: Any,
    container: Container,
    *,
    root_dir: Optional[str] = None,
    exclude_dirs: Optional[Iterable[str]] = None,
    validate: bool = False,
    close_on_shutdown: bool = True,
) -> Any:
    """Connect a container to a Django application with one call.

    Use it where the application object is created, and return its result::

        # wsgi.py
        application = attach_container(get_wsgi_application(), container)

    - Every request runs in its own scope; scoped services are shared within a
      request and closed when the response is finished (WSGI and ASGI).
    - Discovery and activation run once, on the first request.
    - Without ``root_dir`` the project root is the nearest folder above the
      calling file that contains ``manage.py``.
    - With ``close_on_shutdown`` (default) container-created singletons are
      disposed when the process exits. Supplied instances are never disposed.
    """
    if root_dir is None:
        root_dir = caller_root(1, marker="manage.py")
    startup = Startup(container, root_dir, exclude_dirs, validate)
    if is_asgi(application):
        wrapped: Any = AsgiRequestScope(application, container, startup)
    else:
        wrapped = WsgiRequestScope(application, container, startup)
    if close_on_shutdown:
        atexit.register(container._release_singletons_at_exit)
    return wrapped
