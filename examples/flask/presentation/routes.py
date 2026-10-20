from flask import Blueprint

from presentation.product_controller import ProductController

blueprint = Blueprint("products", __name__)


@blueprint.get("/products")
def list_products():
    return ProductController().list_products()
