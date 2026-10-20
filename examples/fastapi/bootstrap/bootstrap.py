"""Startup: how the application comes up.

Creates the container, adds the registrations and connects FastAPI.
"""
from py_autowired import Container
from py_autowired.fastapi import attach_container

from register import register


def bootstrap(app):
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    attach_container(app, container)   # scan at startup (lifespan), scope per request, close at shutdown
