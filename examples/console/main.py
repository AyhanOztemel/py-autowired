from bootstrap import bootstrap
from presentation.product_controller import ProductController

container = bootstrap()

if __name__ == "__main__":
    with container.create_scope():    # one unit of work (scoped objects live here)
        print(ProductController().list_products())
