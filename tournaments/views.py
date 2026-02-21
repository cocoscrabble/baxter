import json
import random

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import Http404, JsonResponse
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

from .datastar_utils import fragment_response, is_datastar
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
        can_edit = user.is_authenticated and self.object.can_edit(user)
        context["can_edit"] = can_edit
        divisions = self.object.divisions.all()
        if not can_edit:
            divisions = divisions.filter(is_test=False)
        context["divisions"] = divisions
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


class VisibleDivisionMixin:
    """Mixin that raises 404 for test divisions when user is not an editor."""

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if obj.is_test:
            user = self.request.user
            if not (user.is_authenticated and obj.tournament.can_edit(user)):
                raise Http404
        return obj


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


class DivisionDetailView(VisibleDivisionMixin, DetailView):
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


class DivisionAllResultsView(VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_all_results.html"
    context_object_name = "division"


class DivisionEntrantsView(VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_entrants.html"
    context_object_name = "division"


class DivisionStandingsView(VisibleDivisionMixin, DetailView):
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

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        if is_datastar(request):
            return fragment_response(
                "tournaments/_standings_content.html", context, request=request
            )
        return self.render_to_response(context)


class DivisionPairingsView(VisibleDivisionMixin, DetailView):
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
                    played = {}
                    for slip in pd.result_slips:
                        key = (slip.round, frozenset({slip.winner_name, slip.loser_name}))
                        played[key] = slip
                    annotated = []
                    for round_num, round_pairings in pairings:
                        round_annotated = []
                        for p in round_pairings:
                            key = (round_num, frozenset({p.first.name, p.second.name}))
                            slip = played.get(key)
                            if slip:
                                scores = {slip.winner_name: slip.winner_score, slip.loser_name: slip.loser_score}
                                result = f"{scores[p.first.name]} - {scores[p.second.name]}"
                            else:
                                result = ""
                            round_annotated.append({"pairing": p, "result": result})
                        annotated.append((round_num, round_annotated))
                    context["pairings"] = annotated
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
        division = get_object_or_404(Division, pk=self.kwargs["pk"])
        if division.is_test:
            user = self.request.user
            if not (user.is_authenticated and division.tournament.can_edit(user)):
                raise Http404
        return division

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


class SimulateMatchView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Simulate a single match for a test division and create a result slip."""

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def post(self, request, pk):
        division = self.get_division()
        if not division.is_test:
            return JsonResponse(
                {"error": "Simulation is only available for test divisions."},
                status=403,
            )

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        round_num = data.get("round")
        first_name = data.get("first")
        second_name = data.get("second")
        if not all([round_num, first_name, second_name]):
            return JsonResponse({"error": "Missing required fields."}, status=400)

        entrants = {
            e.player.name: e
            for e in division.entrants.select_related("player")
        }
        first_entrant = entrants.get(first_name)
        second_entrant = entrants.get(second_name)
        if not first_entrant or not second_entrant:
            return JsonResponse({"error": "Entrant not found."}, status=400)

        r1 = first_entrant.player.rating
        r2 = second_entrant.player.rating
        first_wins = random.random() < r1 / (r1 + r2)

        loser_score = random.randint(200, 450)
        winner_score = random.randint(loser_score, 600)

        if first_wins:
            winner, loser = first_entrant, second_entrant
            winner_started = True
        else:
            winner, loser = second_entrant, first_entrant
            winner_started = False

        ResultSlip.objects.create(
            division=division,
            round=round_num,
            winner=winner,
            winner_score=winner_score,
            loser=loser,
            loser_score=loser_score,
            winner_started=winner_started,
        )
        return JsonResponse({"ok": True})
