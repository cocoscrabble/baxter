import json

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from .forms import (
    ResultSlipForm,
    RoundCountForm,
    RoundPairingFormSet,
    TournamentForm,
)
from .dto import EntrantDTO, ResultSlipDTO
from .models import Division, DivisionSettings, Entrant, Player, ResultSlip, Tournament
from .pairing.base import PairingData, standings_after_round
from .pairing.pair import pair


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
        division = self.object
        max_round = division.max_round()
        context["max_round"] = max_round
        if max_round:
            context["latest_results"] = (
                division.result_slips
                .filter(round=max_round)
                .order_by("-created_at")
            )
        else:
            context["latest_results"] = division.result_slips.none()
        return context


class DivisionAllResultsView(DetailView):
    model = Division
    template_name = "tournaments/division_all_results.html"
    context_object_name = "division"


class DivisionEntrantsView(DetailView):
    model = Division
    template_name = "tournaments/division_entrants.html"
    context_object_name = "division"


class DivisionStandingsView(DetailView):
    model = Division
    template_name = "tournaments/division_standings.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        division = self.object
        pd = PairingData.for_division(division)
        max_round = division.max_round()
        current_round = self.kwargs.get("round", max_round)
        context["standings"] = standings_after_round(pd, current_round)
        context["round"] = current_round
        context["rounds"] = range(1, max_round + 1)
        return context


class DivisionPairingsView(DetailView):
    model = Division
    template_name = "tournaments/division_pairings.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        division = self.object
        try:
            settings = division.settings
            if not settings.round_pairings:
                context["pairings_message"] = "No round pairings configured."
            else:
                pd = PairingData.for_division(division)
                pairings = pair(pd, settings)
                if pairings:
                    context["pairings"] = pairings
                else:
                    context["pairings_message"] = "No upcoming pairings available."
        except DivisionSettings.DoesNotExist:
            context["pairings_message"] = "Division settings have not been configured."
        return context


class DivisionSettingsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_settings_edit.html"

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def _existing_initial(self, division):
        try:
            if division.settings.round_pairings:
                return [
                    {
                        "round": rp["round"],
                        "pairing_type": rp["pairing"],
                        "start_round": rp["start_round"],
                    }
                    for rp in division.settings.round_pairings
                ]
        except DivisionSettings.DoesNotExist:
            pass
        return []

    def _resize_initial(self, existing, num_rounds):
        result = (existing or [])[:num_rounds]
        for i in range(len(result) + 1, num_rounds + 1):
            result.append({"round": i, "pairing_type": "", "start_round": i - 1})
        return result

    def get_initial_data(self, division):
        existing = self._existing_initial(division)
        num_rounds = self.request.GET.get("num_rounds") or self.request.GET.get("rounds")
        if num_rounds:
            return self._resize_initial(existing, int(num_rounds))
        return existing

    def get(self, request, pk):
        division = self.get_division()
        initial = self.get_initial_data(division)
        formset = RoundPairingFormSet(initial=initial)
        round_count_form = RoundCountForm(initial={"num_rounds": len(initial)})
        return render(request, self.template_name, {
            "division": division,
            "formset": formset,
            "round_count_form": round_count_form,
        })

    def post(self, request, pk):
        division = self.get_division()
        formset = RoundPairingFormSet(request.POST)
        if formset.is_valid():
            round_pairings = [
                {
                    "round": form.cleaned_data["round"],
                    "pairing": form.cleaned_data["pairing_type"],
                    "start_round": form.cleaned_data["start_round"],
                }
                for form in formset
            ]
            settings, _ = DivisionSettings.objects.get_or_create(division=division)
            settings.round_pairings = round_pairings
            settings.save()
            return redirect("division_detail", pk=pk)
        return render(request, self.template_name, {
            "division": division,
            "formset": formset,
        })


class DivisionEntrantsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_entrants_edit.html"

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def get(self, request, pk):
        division = self.get_division()
        entrants = division.entrants.select_related("player").order_by("number")
        entrants_json = [
            {"number": e.number, "player": e.player.pk}
            for e in entrants
        ]
        players_json = [
            {"id": p.pk, "label": p.name}
            for p in Player.objects.all()
        ]
        return render(request, self.template_name, {
            "division": division,
            "entrants_json": json.dumps(entrants_json),
            "players_json": json.dumps(players_json),
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get("entrants", [])
        valid_player_ids = set(Player.objects.values_list("pk", flat=True))
        errors = []
        seen_players = set()
        validated = []

        for i, row in enumerate(rows):
            entrant = EntrantDTO.from_json(row)
            if entrant is None:
                errors.append(f"Row {i+1}: all fields are required.")
                continue
            row_errors = entrant.validate(valid_player_ids, seen_players)
            if row_errors:
                errors.extend(f"Row {i+1}: {e}" for e in row_errors)
            else:
                validated.append(entrant)

        if errors:
            return JsonResponse({"errors": errors}, status=400)

        division.entrants.all().delete()
        for entrant in validated:
            Entrant.objects.create(division=division, **entrant.to_db_kwargs())
        return JsonResponse({"ok": True})


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


class DivisionEditResultsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_edit_results.html"

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def get(self, request, pk):
        division = self.get_division()
        results = division.result_slips.select_related(
            "winner", "loser"
        ).order_by("round", "pk")
        results_json = [r.to_dict() for r in results]
        entrants = division.entrants.select_related("player").order_by("number")
        entrants_json = [
            {"id": e.pk, "label": e.player.name}
            for e in entrants
        ]
        return render(request, self.template_name, {
            "division": division,
            "results_json": json.dumps(results_json),
            "entrants_json": json.dumps(entrants_json),
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data if isinstance(data, list) else data.get("results", [])
        entrant_ids = set(division.entrants.values_list("pk", flat=True))
        errors = []
        validated = []
        for i, row in enumerate(rows):
            slip = ResultSlipDTO.from_json(row)
            if slip is None:
                errors.append(f"Row {i+1}: all fields are required.")
                continue
            row_errors = slip.validate(entrant_ids)
            if row_errors:
                errors.extend(f"Row {i+1}: {e}" for e in row_errors)
            else:
                validated.append(slip)

        if errors:
            return JsonResponse({"errors": errors}, status=400)

        division.result_slips.all().delete()
        for slip in validated:
            ResultSlip.objects.create(division=division, **slip.to_db_kwargs())

        return JsonResponse({"ok": True})
