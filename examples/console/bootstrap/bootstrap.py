"""Startup: how the application comes up.

Creates the container, adds the registrations and connects the program.
"""
from py_autowired import Container, auto_inject

from register import register


def bootstrap() -> Container:
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    auto_inject(container)        # scans the project; *_instance fields are filled from now on
    return container
