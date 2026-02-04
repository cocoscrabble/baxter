from django.urls import path

from . import views

urlpatterns = [
    path("", views.tournament_list, name="tournament_list"),
    path("<int:pk>/", views.tournament_detail, name="tournament_detail"),
    path("create/", views.tournament_create, name="tournament_create"),
    path("<int:pk>/edit/", views.tournament_edit, name="tournament_edit"),
    path("<int:pk>/delete/", views.tournament_delete, name="tournament_delete"),
]
