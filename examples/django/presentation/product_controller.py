class ProductController:
    """Entry point used by the user interface. It imports nothing."""

    def __init__(self):
        self.product_service_instance = None      # filled with an IProductService

    def list_products(self) -> dict:
        return self.product_service_instance.list_products()
