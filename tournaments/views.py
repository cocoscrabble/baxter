import json
import random
from itertools import groupby

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
from datastar_py.django import read_signals
from .forms import (
    ResultSlipForm,
    RoundCountForm,
    RoundPairingFormSet,
    TournamentForm,
)
from .dto import EntrantDTO, FixedPairingDTO, FixedTableDTO, ResultSlipDTO
from .models import Division, DivisionSettings, Entrant, FixedPairing, FixedTable, Pairing, Player, ResultSlip, Tournament
from .pairing.base import PairingData, RoundStatus, standings_after_round
from .pairing.pair import can_pair, pair, round_status, STRATEGY_TYPES


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
        if can_edit:
            context["regular_divisions"] = self.object.divisions.filter(is_test=False)
            context["test_divisions"] = self.object.divisions.filter(is_test=True)
            context["deleted_divisions"] = Division.all_objects.filter(
                tournament=self.object, is_deleted=True
            )
        else:
            context["divisions"] = self.object.divisions.filter(is_test=False)
        return context


class TournamentCreateView(LoginRequiredMixin, CreateView):
    model = Tournament
    form_class = TournamentForm
    template_name = "tournaments/tournament_form.html"

    def form_valid(self, form):
        form.instance.owner = self.request.user
        response = super().form_valid(form)
        Division.objects.create(tournament=self.object, name="Open")
        return response

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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["regular_divisions"] = self.object.divisions.filter(is_test=False)
        context["test_divisions"] = self.object.divisions.filter(is_test=True)
        context["deleted_divisions"] = Division.all_objects.filter(
            tournament=self.object, is_deleted=True
        )
        return context

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


class DivisionCreateView(LoginRequiredMixin, View):
    def post(self, request, tournament_pk):
        tournament = get_object_or_404(Tournament, pk=tournament_pk)
        if not tournament.can_edit(request.user):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        name = request.POST.get("name", "").strip()
        is_test = request.POST.get("is_test") == "1"
        if name:
            Division.objects.get_or_create(
                tournament=tournament, name=name, defaults={"is_test": is_test}
            )
        return redirect("tournament_detail", pk=tournament_pk)


class DivisionDeleteView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        tournament_pk = division.tournament.pk
        division.soft_delete()
        return redirect("tournament_detail", pk=tournament_pk)


class DivisionRestoreView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def get_division(self):
        return get_object_or_404(Division.all_objects, pk=self.kwargs["pk"])

    def post(self, request, pk):
        division = self.get_division()
        tournament_pk = division.tournament.pk
        division.restore()
        return redirect("tournament_detail", pk=tournament_pk)


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


def _available_rounds(division):
    """Return list of round numbers that can currently be paired."""
    pd = PairingData.for_division(division)
    if not pd.round_pairings:
        return []
    status = round_status(pd)
    return [rp.round for rp in pd.round_pairings if can_pair(rp, status)]


def _get_fixed_table(fixed_table_lookup, entrant_id, round_num):
    """Return (table_number, is_all) for an entrant in a round, or None.

    Round-specific assignments take priority over 'all' (-1) assignments.
    """
    specific = fixed_table_lookup.get((entrant_id, round_num))
    if specific is not None:
        return (specific, False)
    all_val = fixed_table_lookup.get((entrant_id, -1))
    if all_val is not None:
        return (all_val, True)
    return None


def _resolve_fixed_table(first_ft, second_ft, first_rank, second_rank):
    """Resolve the effective table number when both players have fixed tables.

    Round-specific beats 'all'. If both are the same type, the higher-standing
    (lower rank number) player's table wins.
    """
    if first_ft[1] and not second_ft[1]:
        return second_ft[0]  # second is round-specific
    if second_ft[1] and not first_ft[1]:
        return first_ft[0]   # first is round-specific
    return first_ft[0] if first_rank < second_rank else second_ft[0]


def _regenerate_pairings(division):
    """Run the pairing algorithm and save results to the Pairing table."""
    pd = PairingData.for_division(division)
    if not pd.round_pairings:
        division.pairings.all().delete()
        return
    pairings = pair(pd)
    entrant_by_name = {
        e.player.name: e
        for e in division.entrants.select_related("player")
    }
    start_round_by_round = {rp.round: rp.start_round for rp in pd.round_pairings}
    fixed_table_lookup = {
        (ft.entrant_id, ft.round_number): ft.table_number
        for ft in division.fixed_tables.all()
    }
    division.pairings.all().delete()
    for round_num, round_pairings in pairings:
        start_round = start_round_by_round.get(round_num, 0)
        standings = standings_after_round(pd, start_round)
        rank = {p.name: i + 1 for i, p in enumerate(standings)}

        # Resolve entrants and effective fixed table for each pairing.
        resolved = []
        for p in round_pairings:
            first_entrant = entrant_by_name.get(p.first.name)
            second_entrant = entrant_by_name.get(p.second.name)
            if not first_entrant or not second_entrant:
                continue
            first_ft = _get_fixed_table(fixed_table_lookup, first_entrant.pk, round_num)
            second_ft = _get_fixed_table(fixed_table_lookup, second_entrant.pk, round_num)
            if first_ft and second_ft:
                effective = _resolve_fixed_table(
                    first_ft, second_ft,
                    rank[p.first.name], rank[p.second.name],
                )
            elif first_ft:
                effective = first_ft[0]
            elif second_ft:
                effective = second_ft[0]
            else:
                effective = None
            resolved.append((p, first_entrant, second_entrant, effective))

        # Assign table numbers: fixed pairings keep their numbers, free pairings
        # are sorted by standings and fill the remaining slots.
        n = len(resolved)
        used = {eff for _, _, _, eff in resolved if eff is not None}
        available = [i for i in range(1, n + 1) if i not in used]
        free = sorted(
            [(p, fe, se) for p, fe, se, eff in resolved if eff is None],
            key=lambda x: min(rank[x[0].first.name], rank[x[0].second.name]),
        )
        free_table = dict(zip((id(p) for p, _, _ in free), available))

        for p, first_entrant, second_entrant, effective in resolved:
            table_num = effective if effective is not None else free_table[id(p)]
            Pairing.objects.create(
                division=division,
                round=round_num,
                first=first_entrant,
                second=second_entrant,
                repeats=p.repeats,
                table=table_num,
            )


def _build_pairings_context(division):
    """Build pairings context dict for a division. Reads from the Pairing table."""
    context = {"division": division}
    db_pairings = list(
        division.pairings
        .select_related("first", "first__player", "second", "second__player")
        .order_by("round", "table")
    )
    if not db_pairings:
        context["pairings_message"] = "No pairings generated yet."
        return context
    pd = PairingData.for_division(division)
    status = round_status(pd)
    played = {}
    for slip in division.result_slips.all():
        key = (slip.round, frozenset({slip.winner_id, slip.loser_id}))
        played[key] = slip
    annotated = []
    for round_num, round_pairings in groupby(db_pairings, key=lambda p: p.round):
        if status[round_num] == RoundStatus.Finished:
            continue
        round_annotated = []
        for p in round_pairings:
            key = (round_num, frozenset({p.first_id, p.second_id}))
            slip = played.get(key)
            if slip:
                scores = {slip.winner_id: slip.winner_score, slip.loser_id: slip.loser_score}
                result = f"{scores[p.first_id]} - {scores[p.second_id]}"
            else:
                result = ""
            round_annotated.append({"pairing": p, "result": result})
        annotated.append((round_num, round_annotated))
    context["pairings"] = annotated
    return context


class GeneratePairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        _regenerate_pairings(division)
        return redirect("division_pairings", pk=pk)


class DivisionPairingsView(VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_pairings.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_build_pairings_context(self.object))
        user = self.request.user
        can_edit = user.is_authenticated and self.object.tournament.can_edit(user)
        context["can_edit"] = can_edit
        if can_edit:
            context["available_rounds"] = _available_rounds(self.object)
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
        context = {
            "division": division,
            "formset": formset,
            "round_count_form": round_count_form,
            "strategy_types": STRATEGY_TYPES,
        }
        if is_datastar(request):
            return fragment_response(
                "tournaments/_settings_formset.html", context, request=request
            )
        return render(request, self.template_name, context)

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


class DivisionFixedPairingsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_fixed_pairings_edit.html"

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def get(self, request, pk):
        division = self.get_division()
        entrants = division.entrants.select_related("player").order_by("number")
        entrant_values = [{"id": e.pk, "label": e.player.name} for e in entrants]
        existing = [
            {"round_number": fp.round_number, "entrant1": fp.entrant1_id, "entrant2": fp.entrant2_id}
            for fp in division.fixed_pairings.all()
        ]
        return render(request, self.template_name, {
            "division": division,
            "entrant_values_json": json.dumps(entrant_values),
            "fixed_pairings_json": json.dumps(existing),
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get("pairings", [])
        valid_entrant_ids = set(division.entrants.values_list("pk", flat=True))
        errors = []
        seen_per_round: dict[int, set[int]] = {}
        validated = []

        for i, row in enumerate(rows):
            fp = FixedPairingDTO.from_json(row)
            if fp is None:
                errors.append(f"Row {i+1}: all fields are required.")
                continue
            row_errors = fp.validate(valid_entrant_ids, seen_per_round)
            if row_errors:
                errors.extend(f"Row {i+1}: {e}" for e in row_errors)
            else:
                validated.append(fp)

        if errors:
            return JsonResponse({"errors": errors}, status=400)

        division.fixed_pairings.all().delete()
        for fp in validated:
            FixedPairing.objects.create(division=division, **fp.to_db_kwargs())
        return JsonResponse({"ok": True})


class DivisionFixedTablesEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_fixed_tables_edit.html"

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def get(self, request, pk):
        division = self.get_division()
        entrants = division.entrants.select_related("player").order_by("number")
        entrant_values = [{"id": e.pk, "label": e.player.name} for e in entrants]
        existing = [
            {"round_number": ft.round_number, "entrant": ft.entrant_id, "table_number": ft.table_number}
            for ft in division.fixed_tables.all()
        ]
        try:
            rps = division.settings.round_pairings
            round_numbers = sorted(set(rp["round"] for rp in rps))
        except (AttributeError, KeyError, TypeError):
            round_numbers = list(range(1, 16))
        round_values = [{"value": -1, "label": "All"}] + [{"value": r, "label": str(r)} for r in round_numbers]
        return render(request, self.template_name, {
            "division": division,
            "entrant_values_json": json.dumps(entrant_values),
            "fixed_tables_json": json.dumps(existing),
            "round_values_json": json.dumps(round_values),
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get("tables", [])
        valid_entrant_ids = set(division.entrants.values_list("pk", flat=True))
        errors = []
        seen_per_round: dict[int, set[int]] = {}
        validated = []

        for i, row in enumerate(rows):
            ft = FixedTableDTO.from_json(row)
            if ft is None:
                errors.append(f"Row {i+1}: all fields are required.")
                continue
            row_errors = ft.validate(valid_entrant_ids, seen_per_round)
            if row_errors:
                errors.extend(f"Row {i+1}: {e}" for e in row_errors)
            else:
                validated.append(ft)

        if errors:
            return JsonResponse({"errors": errors}, status=400)

        division.fixed_tables.all().delete()
        for ft in validated:
            FixedTable.objects.create(division=division, **ft.to_db_kwargs())
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
        division = self.get_division()
        kwargs["division"] = division
        kwargs["round_numbers"] = self._round_numbers(division)
        return kwargs

    def _player_names(self, division):
        return list(
            Entrant.objects.filter(division=division)
            .select_related("player")
            .values_list("player__name", flat=True)
            .order_by("player__name")
        )

    def _round_numbers(self, division):
        try:
            rps = division.settings.round_pairings
            all_rounds = sorted(set(rp["round"] for rp in rps))
        except (AttributeError, KeyError, TypeError):
            all_rounds = list(range(1, 16))
        pd = PairingData.for_division(division)
        status = round_status(pd)
        return [r for r in all_rounds if status[r] != RoundStatus.Finished]

    def post(self, request, *args, **kwargs):
        if is_datastar(request):
            division = self.get_division()
            round_numbers = self._round_numbers(division)
            signals = read_signals(request) or {}
            form = ResultSlipForm(signals, division=division, round_numbers=round_numbers)
            player_names = self._player_names(division)
            if form.is_valid():
                form.instance.division = division
                form.save()
                fresh_form = ResultSlipForm(division=division, round_numbers=self._round_numbers(division))
                return fragment_response(
                    "tournaments/_resultslip_form.html",
                    {"form": fresh_form, "division": division, "player_names": player_names, "success_message": "Result saved. If there are any mistakes, edit the form and click save again. If everything looks correct, hit Done to close the form."},
                    request=request,
                )
            return fragment_response(
                "tournaments/_resultslip_form.html",
                {"form": form, "division": division, "player_names": player_names},
                request=request,
            )
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        form.instance.division = self.get_division()
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("division_detail", kwargs={"pk": self.kwargs["pk"]})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        division = self.get_division()
        context["division"] = division
        context["player_names"] = self._player_names(division)
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

        if is_datastar(request):
            data = read_signals(request) or {}
        else:
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

        if is_datastar(request):
            context = _build_pairings_context(division)
            return fragment_response(
                "tournaments/_pairings_content.html", context, request=request
            )
        return JsonResponse({"ok": True})


class SimulateRoundView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Simulate all remaining matches in a round for a test division."""

    def get_division(self):
        return get_object_or_404(Division, pk=self.kwargs["pk"])

    def post(self, request, pk):
        division = self.get_division()
        if not division.is_test:
            return JsonResponse(
                {"error": "Simulation is only available for test divisions."},
                status=403,
            )

        if is_datastar(request):
            data = read_signals(request) or {}
        else:
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({"error": "Invalid JSON."}, status=400)

        round_num = data.get("round")
        if not round_num:
            return JsonResponse({"error": "Missing required fields."}, status=400)

        played = frozenset(
            frozenset({slip.winner_id, slip.loser_id})
            for slip in division.result_slips.filter(round=round_num)
        )

        pairings = division.pairings.filter(round=round_num).select_related(
            "first__player", "second__player"
        )

        for pairing in pairings:
            if frozenset({pairing.first_id, pairing.second_id}) in played:
                continue
            r1 = pairing.first.player.rating
            r2 = pairing.second.player.rating
            first_wins = random.random() < r1 / (r1 + r2)
            loser_score = random.randint(200, 450)
            winner_score = random.randint(loser_score, 600)
            if first_wins:
                winner, loser = pairing.first, pairing.second
                winner_started = True
            else:
                winner, loser = pairing.second, pairing.first
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

        if is_datastar(request):
            context = _build_pairings_context(division)
            return fragment_response(
                "tournaments/_pairings_content.html", context, request=request
            )
        return JsonResponse({"ok": True})
