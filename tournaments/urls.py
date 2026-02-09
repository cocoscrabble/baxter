from django.urls import path

from .views import (
    DivisionDetailView,
    DivisionRoundCountView,
    DivisionSettingsEditView,
    DivisionSettingsView,
    DivisionStandingsView,
    ResultSlipCreateView,
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
    path("division/<int:pk>/", DivisionDetailView.as_view(), name="division_detail"),
    path("division/<int:pk>/add-result/", ResultSlipCreateView.as_view(), name="resultslip_create"),
    path("division/<int:pk>/standings/", DivisionStandingsView.as_view(), name="division_standings"),
    path("division/<int:pk>/standings/<int:round>/", DivisionStandingsView.as_view(), name="division_standings_round"),
    path("division/<int:pk>/settings/", DivisionSettingsView.as_view(), name="division_settings"),
    path("division/<int:pk>/settings/rounds/", DivisionRoundCountView.as_view(), name="division_settings_rounds"),
    path("division/<int:pk>/settings/edit/", DivisionSettingsEditView.as_view(), name="division_settings_edit"),
]
