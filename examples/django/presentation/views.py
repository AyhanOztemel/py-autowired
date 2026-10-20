from django.http import JsonResponse

from presentation.product_controller import ProductController


def list_products(request):
    return JsonResponse(ProductController().list_products())
