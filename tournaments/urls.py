from django.urls import path

from .views import (
    TournamentCreateView,
    TournamentDeleteView,
    TournamentDetailView,
    TournamentListView,
    TournamentUpdateView,
)

urlpatterns = [
    path("", TournamentListView.as_view(), name="tournament_list"),
    path("<int:pk>/", TournamentDetailView.as_view(), name="tournament_detail"),
    path("create/", TournamentCreateView.as_view(), name="tournament_create"),
    path("<int:pk>/edit/", TournamentUpdateView.as_view(), name="tournament_edit"),
    path("<int:pk>/delete/", TournamentDeleteView.as_view(), name="tournament_delete"),
]
