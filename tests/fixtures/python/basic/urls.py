"""Django-style registrations."""

from django.urls import include, path, re_path

from . import views

urlpatterns = [
    path("a/<int:pk>", views.detail),
    re_path(r"^old/(?P<slug>[\w-]+)/$", views.old),
    path("v1/", include("sub.urls")),
    path("classes/<slug:name>", views.ClassView.as_view()),
]
