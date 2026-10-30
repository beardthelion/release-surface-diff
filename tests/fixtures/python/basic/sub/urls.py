"""Django urlpatterns mounted under v1/ by the parent urls.py."""

from django.urls import path

from . import views

urlpatterns = [
    path("x/<int:k>", views.xview),
]
