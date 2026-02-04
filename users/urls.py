from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from .views import RegisterView, profile_view

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path(
        "login/",
        LoginView.as_view(template_name="users/login.html"),
        name="login",
    ),
    path(
        "logout/",
        LogoutView.as_view(template_name="users/logout.html"),
        name="logout",
    ),
    path("profile/", profile_view, name="profile"),
]
