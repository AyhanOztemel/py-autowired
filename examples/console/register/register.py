"""Registrations: which contracts the application uses and how long each object lives.

Only contracts (interfaces / abstract classes) are named here. py_autowired
finds the single implementation of each contract in the project by itself.
If a contract has more than one implementation, name the one to use:
    container.register_scoped(IProductRepository, SqlProductRepository)

  singleton - one object for the whole application
  scoped    - one object per scope (one unit of work)
  transient - a new object every time it is needed
"""
from domain.clock import IClock
from domain.product_repository import IProductRepository
from domain.product_service import IProductService


def register(container):
    container.register_singleton(IClock)
    container.register_scoped(IProductRepository)
    container.register_transient(IProductService)
