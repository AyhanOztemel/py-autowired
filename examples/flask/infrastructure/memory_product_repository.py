from domain.product_repository import IProductRepository


class MemoryProductRepository(IProductRepository):
    """Keeps products in memory. A real application would use a database here."""

    def __init__(self):
        self._products = ["Notebook", "Pencil", "Eraser"]

    def list_products(self) -> list[str]:
        return list(self._products)

    def close(self):
        # Called automatically when its scope ends (for a web app: the request).
        self._products.clear()
