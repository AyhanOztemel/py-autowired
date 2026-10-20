"""Startup: how the application comes up.

Creates the container, adds the registrations and connects Django.
"""
from py_autowired import Container
from py_autowired.django import attach_container

from register import register


def bootstrap(application):
    container = Container(strict_interfaces=True)   # only contracts can be registered
    register(container)
    return attach_container(application, container)   # scope per request, close at exit
