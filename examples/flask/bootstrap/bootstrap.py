"""Startup: how the application comes up.

Creates the container, adds the registrations and connects Flask.
"""
from py_autowired import Container
from py_autowired.flask import attach_container

from register import register


def bootstrap(app):
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    attach_container(app, container)   # scan on the first request, scope per request, close at exit
