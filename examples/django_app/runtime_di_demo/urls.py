from django.urls import path
from examples.django_app.runtime_di_demo.views import di_demo, index


urlpatterns = [
    path("", index),
    path("di-demo/<str:name>", di_demo),
    path("hello/<str:name>/", di_demo),
]
