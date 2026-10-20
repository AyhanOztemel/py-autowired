"""Registrations: the ONLY place that imports the application classes.

Every line pairs a contract (interface / abstract class) with the class that
implements it, and says how long each object lives:
  singleton - one object for the whole application
  scoped    - one object per scope (one unit of work)
  transient - a new object every time it is needed
"""
from application.product_service import ProductService
from domain.clock import IClock
from domain.product_repository import IProductRepository
from domain.product_service import IProductService
from infrastructure.memory_product_repository import MemoryProductRepository
from infrastructure.system_clock import SystemClock


def register(container):
    container.register_singleton(IClock, SystemClock)
    container.register_scoped(IProductRepository, MemoryProductRepository)
    container.register_transient(IProductService, ProductService)
