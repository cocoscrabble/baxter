from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from .forms import ResultSlipForm, TournamentForm
from .models import Division, Tournament


class TournamentListView(ListView):
    model = Tournament
    template_name = "tournaments/tournament_list.html"
    context_object_name = "tournaments"


class TournamentDetailView(DetailView):
    model = Tournament
    template_name = "tournaments/tournament_detail.html"
    context_object_name = "tournament"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["can_edit"] = (
            user.is_authenticated and self.object.can_edit(user)
        )
        return context


class TournamentCreateView(LoginRequiredMixin, CreateView):
    model = Tournament
    form_class = TournamentForm
    template_name = "tournaments/tournament_form.html"

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)

    def get_success_url(self):
        return self.object.get_absolute_url()


class CanEditTournamentMixin(UserPassesTestMixin):
    """Mixin that checks if user can edit the tournament."""

    def test_func(self):
        tournament = self.get_object()
        return tournament.can_edit(self.request.user)


class CanEditDivisionMixin(UserPassesTestMixin):
    """Mixin that checks if user can edit the division's tournament."""

    def test_func(self):
        division = self.get_division()
        return division.tournament.can_edit(self.request.user)

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])


class TournamentUpdateView(LoginRequiredMixin, CanEditTournamentMixin, UpdateView):
    model = Tournament
    form_class = TournamentForm
    template_name = "tournaments/tournament_form.html"
    context_object_name = "tournament"

    def get_success_url(self):
        return self.object.get_absolute_url()


class IsOwnerMixin(UserPassesTestMixin):
    """Mixin that checks if user is the tournament owner."""

    def test_func(self):
        tournament = self.get_object()
        return self.request.user == tournament.owner


class TournamentDeleteView(LoginRequiredMixin, IsOwnerMixin, DeleteView):
    model = Tournament
    template_name = "tournaments/tournament_confirm_delete.html"
    context_object_name = "tournament"
    success_url = reverse_lazy("tournament_list")


class DivisionDetailView(DetailView):
    model = Division
    template_name = "tournaments/division_detail.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        context["can_edit"] = (
            user.is_authenticated and self.object.tournament.can_edit(user)
        )
        return context


class ResultSlipCreateView(CreateView):
    model = Division
    form_class = ResultSlipForm
    template_name = "tournaments/resultslip_form.html"

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["division"] = self.get_division()
        return kwargs

    def form_valid(self, form):
        form.instance.division = self.get_division()
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("division_detail", kwargs={"pk": self.kwargs["pk"]})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["division"] = self.get_division()
        return context
