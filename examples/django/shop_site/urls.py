from django.urls import path

from presentation.views import list_products

urlpatterns = [path("products", list_products)]
