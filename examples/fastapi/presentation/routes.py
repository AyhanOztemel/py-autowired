from fastapi import APIRouter

from presentation.product_controller import ProductController

router = APIRouter()


@router.get("/products")
def list_products():
    return ProductController().list_products()
