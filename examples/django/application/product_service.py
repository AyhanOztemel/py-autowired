from domain.product_service import IProductService


class ProductService(IProductService):
    """Business logic. Its only import is the contract it implements;
    its dependencies arrive at runtime."""

    def __init__(self):
        self.product_repository_instance = None   # filled with an IProductRepository
        self.clock_instance = None                # filled with an IClock

    def list_products(self) -> dict:
        return {
            "products": self.product_repository_instance.list_products(),
            "time": self.clock_instance.now(),
        }
