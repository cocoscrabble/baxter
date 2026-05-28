import json
from itertools import groupby

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import models
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
from .dto import EntrantDTO, FixedPairingDTO, FixedTableDTO, ResultSlipDTO, parse_rows
from .fixed_pairings import add_fixed_pairing, remove_fixed_pairings
from .match_simulation import simulate_match, simulate_round
from .models import Division, DivisionSettings, Entrant, FixedPairing, FixedTable, Pairing, Player, ResultSlip, RoundPairings, Tournament
from .generate_pairings import regenerate_pairings
from .pairing.base import PairingData, RoundStatus, standings_after_round
from .pairing.pair import can_pair, round_status, STRATEGY_TYPES


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
        can_edit = self.object.can_edit(user)
        context["can_edit"] = can_edit
        if can_edit:
            context.update(self.object.division_buckets())
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


def _ensure_visible_division(division, user):
    """Raise Http404 if this is a test division the user is not allowed to see."""
    if division.is_test and not division.tournament.can_edit(user):
        raise Http404


class VisibleDivisionMixin:
    """Mixin that raises 404 for test divisions when user is not an editor."""

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        _ensure_visible_division(obj, self.request.user)
        return obj


class DivisionNavMixin:
    """Adds active_tab and can_edit to context for the division navbar."""

    active_tab = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        division = self.object
        user = self.request.user
        if "can_edit" not in context:
            context["can_edit"] = division.tournament.can_edit(user)
        context["active_tab"] = self.active_tab
        return context


class TournamentUpdateView(LoginRequiredMixin, CanEditTournamentMixin, UpdateView):
    model = Tournament
    form_class = TournamentForm
    template_name = "tournaments/tournament_form.html"
    context_object_name = "tournament"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.object.division_buckets())
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


class DivisionDetailView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_detail.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
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


class DivisionAllResultsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_all_results.html"
    context_object_name = "division"
    active_tab = "results"


class DivisionEntrantsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_entrants.html"
    context_object_name = "division"
    active_tab = "entrants"


class DivisionStandingsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_standings.html"
    context_object_name = "division"
    active_tab = "standings"

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
    """Return list of round numbers that can currently be (re)paired.

    A round is available if it can be paired based on results AND its
    persisted RoundPairings status is DRAFT (or absent). Published,
    in-progress, and finished rounds are locked.
    """
    pd = PairingData.for_division(division)
    if not pd.round_pairings:
        return []
    status = round_status(pd)
    locked_rounds = set(
        division.round_pairings_set
        .exclude(status=RoundPairings.DRAFT)
        .values_list("round", flat=True)
    )
    return [
        rp.round for rp in pd.round_pairings
        if can_pair(rp, status) and rp.round not in locked_rounds
    ]


def _waiting_message(division):
    """Return a message describing why no rounds can be paired, or None."""
    pd = PairingData.for_division(division)
    if not pd.round_pairings:
        return "No round pairings configured."
    status = round_status(pd)
    for rp in pd.round_pairings:
        stat = status[rp.round]
        if stat in (RoundStatus.Finished, RoundStatus.Partial):
            continue
        # This is the next round that needs pairing.
        if rp.start_round and status[rp.start_round] != RoundStatus.Finished:
            return f"Round {rp.round} is waiting for round {rp.start_round} results."
        return None
    return "All rounds are finished."



def _pairings_common(division):
    """Load pairings, result slips, fixed pairings, and round statuses for a division."""
    db_pairings = list(
        division.pairings
        .select_related("first", "first__player", "second", "second__player")
        .order_by("round", "table")
    )
    pd = PairingData.for_division(division)
    status = round_status(pd)
    played = {}
    for slip in division.result_slips.all():
        key = (slip.round, frozenset({slip.winner_id, slip.loser_id}))
        played[key] = slip
    fixed_lookup = {}
    for fp in division.fixed_pairings.all():
        fixed_lookup[(fp.round_number, frozenset({fp.entrant1_id, fp.entrant2_id}))] = fp.pk
    return db_pairings, status, played, fixed_lookup


def _annotate_round(round_pairings, round_num, played, fixed_lookup):
    """Build annotated pairing list for a single round."""
    annotated = []
    for p in round_pairings:
        key = (round_num, frozenset({p.first_id, p.second_id}))
        slip = played.get(key)
        if slip:
            scores = {slip.winner_id: slip.winner_score, slip.loser_id: slip.loser_score}
            result = f"{scores[p.first_id]} - {scores[p.second_id]}"
        else:
            result = ""
        fixed_id = fixed_lookup.get(key)
        annotated.append({"pairing": p, "result": result, "is_fixed": bool(fixed_id), "fixed_id": fixed_id})
    return annotated


def _round_tabs(division):
    """Build a list of round tab dicts with status for the pairings tab bar.

    Each tab has: round (int), status (str), label (str for future rounds).
    Statuses: 'finished', 'in_progress', 'published', 'pairable', 'future',
    'error_no_pairings'.
    """
    pd = PairingData.for_division(division)
    if not pd.round_pairings:
        return []

    statuses = round_status(pd)
    rp_status_map = dict(
        division.round_pairings_set.values_list("round", "status")
    )
    rounds_with_pairings = set(
        division.pairings.values_list("round", flat=True).distinct()
    )
    # Build settings lookup for future round descriptions.
    settings_lookup = {rp.round: rp for rp in pd.round_pairings}

    tabs = []
    for rp in pd.round_pairings:
        r = rp.round
        db_status = rp_status_map.get(r)
        if db_status == RoundPairings.FINISHED:
            tab_status = "finished"
        elif db_status == RoundPairings.IN_PROGRESS:
            tab_status = "in_progress"
        elif db_status == RoundPairings.PUBLISHED:
            tab_status = "published"
        elif statuses.get(r) == RoundStatus.Finished:
            tab_status = "finished"
        elif statuses.get(r) == RoundStatus.Partial:
            tab_status = "in_progress"
        elif can_pair(rp, statuses):
            tab_status = "pairable"
        else:
            tab_status = "future"
        # An in-progress round with no Pairing records can't render properly —
        # we have results but don't know the unplayed pairings.
        if tab_status == "in_progress" and r not in rounds_with_pairings:
            tab_status = "error_no_pairings"
        setting = settings_lookup.get(r)
        label = ""
        if setting:
            label = setting.pairing
            if setting.start_round:
                label += f" (from round {setting.start_round})"
        tabs.append({"round": r, "status": tab_status, "label": label})
    return tabs


def _build_pairings_context(division):
    """Build pairings context dict for a division with tabbed round view."""
    context = {"division": division}
    tabs = _round_tabs(division)
    context["round_tabs"] = tabs
    if not tabs:
        context["pairings_message"] = "No round pairings configured."
        return context

    db_pairings, status, played, fixed_lookup = _pairings_common(division)

    # Default selected round: first in-progress, then published, then pairable,
    # then first finished (latest), else first.
    selected = None
    for preference in ("in_progress", "published", "pairable"):
        for tab in tabs:
            if tab["status"] == preference:
                selected = tab["round"]
                break
        if selected:
            break
    if not selected:
        finished = [t for t in tabs if t["status"] == "finished"]
        if finished:
            selected = finished[-1]["round"]
        else:
            selected = tabs[0]["round"]
    context["selected_round"] = selected

    # Build content for the selected round.
    context.update(_build_round_content(
        division, selected, tabs, db_pairings, played, fixed_lookup
    ))

    context["has_draft_rounds"] = division.round_pairings_set.filter(
        status=RoundPairings.DRAFT
    ).exists()
    context["has_published_rounds"] = division.round_pairings_set.filter(
        status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS]
    ).exists()
    return context


def _build_round_content(division, round_num, tabs, db_pairings, played, fixed_lookup):
    """Build the content context for a single round tab."""
    context = {}
    tab = next((t for t in tabs if t["round"] == round_num), None)
    if not tab:
        return context
    context["selected_round"] = round_num
    context["selected_status"] = tab["status"]
    context["round_label"] = tab["label"]

    if tab["status"] in ("finished", "in_progress", "error_no_pairings"):
        # Show pairings with results.
        round_pairings = [p for p in db_pairings if p.round == round_num]
        if round_pairings:
            context["round_pairings"] = _annotate_round(
                round_pairings, round_num, played, fixed_lookup
            )
        else:
            # Fallback: build from result slips when no Pairing objects exist
            # (e.g. legacy data or simulation-seeded slips).
            slips = list(
                division.result_slips
                .filter(round=round_num)
                .select_related("winner__player", "loser__player")
                .order_by("pk")
            )
            fp_lookup = {}
            for fp in division.fixed_pairings.filter(round_number=round_num):
                fp_lookup[frozenset({fp.entrant1_id, fp.entrant2_id})] = fp.pk
            context["round_pairings"] = [
                {
                    "first_name": slip.winner.player.name,
                    "second_name": slip.loser.player.name,
                    "result": f"{slip.winner_score} - {slip.loser_score}",
                    "is_fixed": frozenset({slip.winner_id, slip.loser_id}) in fp_lookup,
                    "from_slips": True,
                }
                for slip in slips
            ]
    elif tab["status"] in ("pairable", "published"):
        # Show pairings if generated, plus generate/publish controls.
        round_pairings = [p for p in db_pairings if p.round == round_num]
        if round_pairings:
            context["round_pairings"] = _annotate_round(
                round_pairings, round_num, played, fixed_lookup
            )
    # For 'future' status, round_label is already set — template shows settings text.
    return context


def _build_single_round_context(division, round_num):
    """Build context for a Datastar fragment request for a single round."""
    context = {"division": division}
    tabs = _round_tabs(division)
    context["round_tabs"] = tabs
    db_pairings, status, played, fixed_lookup = _pairings_common(division)
    context.update(_build_round_content(
        division, round_num, tabs, db_pairings, played, fixed_lookup
    ))
    context["has_draft_rounds"] = division.round_pairings_set.filter(
        status=RoundPairings.DRAFT
    ).exists()
    context["has_published_rounds"] = division.round_pairings_set.filter(
        status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS]
    ).exists()
    return context


class GeneratePairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        regenerate_pairings(division)
        return redirect("division_pairings", pk=pk)


class PublishPairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        division.round_pairings_set.filter(
            status=RoundPairings.DRAFT
        ).update(status=RoundPairings.PUBLISHED)
        return redirect("division_pairings", pk=pk)


class AddFixedPairingView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        round_number = int(request.POST["round"])
        entrant1_id = int(request.POST["entrant1"])
        entrant2_id = int(request.POST["entrant2"])
        _, error = add_fixed_pairing(division, round_number, entrant1_id, entrant2_id)
        if error:
            messages.error(request, error)
        return redirect("division_pairings", pk=pk)


class RemoveFixedPairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        keep_ids = set(request.POST.getlist("keep"))
        error = remove_fixed_pairings(division, keep_ids)
        if error:
            messages.error(request, error)
        return redirect("division_pairings", pk=pk)


class DivisionPairingsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_pairings.html"
    context_object_name = "division"
    active_tab = "pairings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_build_pairings_context(self.object))
        can_edit = context["can_edit"]
        if can_edit:
            rounds = _available_rounds(self.object)
            if rounds:
                plural = "rounds" if len(rounds) > 1 else "round"
                context["generate_label"] = f"Generate Pairings ({plural} {', '.join(str(r) for r in rounds)})"
            else:
                context["waiting_message"] = _waiting_message(self.object)
            context["entrants"] = list(
                self.object.entrants.select_related("player").order_by("player__name")
            )
        return context


class RoundPairingsTabView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    """Datastar fragment endpoint for switching between round tabs."""
    model = Division
    template_name = "tournaments/division_pairings.html"
    context_object_name = "division"
    active_tab = "pairings"

    def get(self, request, pk, round):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        context.update(_build_single_round_context(self.object, round))
        can_edit = context.get("can_edit", False)
        if can_edit:
            context["entrants"] = list(
                self.object.entrants.select_related("player").order_by("player__name")
            )
        if is_datastar(request):
            return fragment_response(
                "tournaments/_round_tab_content.html", context, request=request
            )
        return self.render_to_response(context)


def _build_published_pairings_context(division):
    """Build context for the published pairings page.

    Shows rounds that are published or in-progress. Finished rounds are
    dropped — their results live on the results/standings pages.
    """
    context = {"division": division}
    published_rounds = set(
        division.round_pairings_set
        .filter(status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS])
        .values_list("round", flat=True)
    )
    if not published_rounds:
        context["pairings_message"] = "No pairings published yet."
        return context
    db_pairings = list(
        division.pairings
        .filter(round__in=published_rounds)
        .select_related("first", "first__player", "second", "second__player")
        .order_by("round", "table")
    )
    if not db_pairings:
        context["pairings_message"] = "No pairings published yet."
        return context
    annotated = []
    for round_num, round_pairings in groupby(db_pairings, key=lambda p: p.round):
        round_annotated = [{"pairing": p} for p in round_pairings]
        annotated.append((round_num, round_annotated))
    context["pairings"] = annotated
    return context


class PublishedPairingsView(VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/published_pairings.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(_build_published_pairings_context(self.object))
        return context


class DivisionSettingsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_settings_edit.html"

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
            "active_tab": "settings",
            "can_edit": True,
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
            "active_tab": "edit_entrants",
            "can_edit": True,
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get("entrants", [])
        valid_player_ids = set(Player.objects.values_list("pk", flat=True))
        seen_players: set[int] = set()
        validated, errors = parse_rows(
            EntrantDTO, rows, valid_player_ids, seen_players
        )
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        division.entrants.all().delete()
        for entrant in validated:
            Entrant.objects.create(division=division, **entrant.to_db_kwargs())
        return JsonResponse({"ok": True})


class CreatePlayerView(LoginRequiredMixin, View):
    """AJAX endpoint to create a new Player and return its data."""

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        player, error = Player.create_unique(
            name=data.get("name"), rating=data.get("rating", 0)
        )
        if error:
            return JsonResponse({"error": error}, status=400)
        return JsonResponse({
            "ok": True,
            "id": player.pk,
            "label": player.name,
            "player_number": player.player_number,
        })


class BulkImportEntrantsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Import entrants from a CSV file, creating new Players as needed."""

    def post(self, request, pk):
        from tournaments.import_entrants import import_entrants

        division = self.get_division()
        uploaded = request.FILES.get("csv_file")
        if not uploaded:
            return JsonResponse({"errors": ["No file uploaded."]}, status=400)

        try:
            text = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return JsonResponse({"errors": ["File must be UTF-8 encoded text."]}, status=400)

        result, errors = import_entrants(division, text)
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        return JsonResponse({
            "ok": True,
            "created": result.created,
            "matched": result.matched,
            "skipped": result.skipped,
            "added": result.added,
        })


class DivisionFixedPairingsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_fixed_pairings_edit.html"

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
            "active_tab": "fixed_pairings",
            "can_edit": True,
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get("pairings", [])
        valid_entrant_ids = set(division.entrants.values_list("pk", flat=True))
        seen_per_round: dict[int, set[int]] = {}
        validated, errors = parse_rows(
            FixedPairingDTO, rows, valid_entrant_ids, seen_per_round
        )
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        division.fixed_pairings.all().delete()
        for fp in validated:
            FixedPairing.objects.create(division=division, **fp.to_db_kwargs())
        return JsonResponse({"ok": True})


class DivisionFixedTablesEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_fixed_tables_edit.html"

    def get(self, request, pk):
        division = self.get_division()
        entrants = division.entrants.select_related("player").order_by("number")
        entrant_values = [{"id": e.pk, "label": e.player.name} for e in entrants]
        existing = [
            {"round_number": ft.round_number, "entrant": ft.entrant_id, "table_number": ft.table_number}
            for ft in division.fixed_tables.all()
        ]
        round_numbers = division.configured_round_numbers()
        round_values = [{"value": -1, "label": "All"}] + [{"value": r, "label": str(r)} for r in round_numbers]
        return render(request, self.template_name, {
            "division": division,
            "entrant_values_json": json.dumps(entrant_values),
            "fixed_tables_json": json.dumps(existing),
            "round_values_json": json.dumps(round_values),
            "active_tab": "fixed_tables",
            "can_edit": True,
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get("tables", [])
        valid_entrant_ids = set(division.entrants.values_list("pk", flat=True))
        seen_per_round: dict[int, set[int]] = {}
        validated, errors = parse_rows(
            FixedTableDTO, rows, valid_entrant_ids, seen_per_round
        )
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        division.fixed_tables.all().delete()
        for ft in validated:
            FixedTable.objects.create(division=division, **ft.to_db_kwargs())
        return JsonResponse({"ok": True})


class DivisionBoardTableMapEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_board_table_map_edit.html"

    def get(self, request, pk):
        division = self.get_division()
        settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
        existing = settings_obj.board_table_map or []
        n_entrants = division.entrants.count()
        default_board_count = (n_entrants + 1) // 2
        return render(request, self.template_name, {
            "division": division,
            "board_table_map_json": json.dumps(existing),
            "default_board_count": default_board_count,
            "active_tab": "board_tables",
            "can_edit": True,
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data.get("rows", [])
        errors = []
        seen_boards = set()
        validated = []
        for i, row in enumerate(rows):
            try:
                board = int(row["board"])
                table = int(row["table"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"Row {i+1}: board and table must be integers.")
                continue
            if board < 1 or table < 1:
                errors.append(f"Row {i+1}: board and table must be positive.")
                continue
            if board in seen_boards:
                errors.append(f"Row {i+1}: duplicate board {board}.")
                continue
            seen_boards.add(board)
            validated.append({"board": board, "table": table})

        if errors:
            return JsonResponse({"errors": errors}, status=400)

        validated.sort(key=lambda r: r["board"])
        settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
        settings_obj.board_table_map = validated
        settings_obj.save(update_fields=["board_table_map"])
        return JsonResponse({"ok": True})


def _pairings_by_round(division):
    """Build pairings data grouped by round for the ResultSlipForm.

    Returns {round_num: [(pairing_pk, first_pk, first_name, second_pk, second_name), ...]}
    Only includes rounds with published/in_progress status and pairings without results.
    """
    rp_objs = (
        division.round_pairings_set
        .filter(status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS])
        .prefetch_related(
            models.Prefetch(
                "pairings",
                queryset=Pairing.objects.select_related(
                    "first__player", "second__player"
                ).order_by("table"),
            )
        )
        .order_by("round")
    )
    result = {}
    for rp in rp_objs:
        pairing_list = []
        for p in rp.pairings.all():
            if hasattr(p, "result"):
                continue
            pairing_list.append((
                p.pk,
                p.first_id,
                p.first.player.name,
                p.second_id,
                p.second.player.name,
            ))
        if pairing_list:
            result[rp.round] = pairing_list
    return result


class ResultSlipCreateView(View):
    template_name = "tournaments/resultslip_form.html"

    def get_division(self):
        division = get_object_or_404(Division, pk=self.kwargs["pk"])
        _ensure_visible_division(division, self.request.user)
        return division

    def _form_context(self, division, form, success_message=None):
        pbr = form._pairings_by_round
        # Build JSON-safe pairings data for datastar client-side filtering.
        pairings_json = {}
        for r, pairing_list in pbr.items():
            pairings_json[str(r)] = [
                {"pk": p_pk, "first_pk": f_pk, "first_name": f_name,
                 "second_pk": s_pk, "second_name": s_name}
                for p_pk, f_pk, f_name, s_pk, s_name in pairing_list
            ]
        context = {
            "form": form,
            "division": division,
            "pairings_json": json.dumps(pairings_json),
            "active_tab": "add_result",
            "can_edit": division.tournament.can_edit(self.request.user),
        }
        if success_message:
            context["success_message"] = success_message
        return context

    def get(self, request, pk):
        division = self.get_division()
        pbr = _pairings_by_round(division)
        form = ResultSlipForm(division=division, pairings_by_round=pbr)
        context = self._form_context(division, form)
        return render(request, self.template_name, context)

    def post(self, request, pk):
        division = self.get_division()
        pbr = _pairings_by_round(division)
        if is_datastar(request):
            data = read_signals(request) or {}
        else:
            data = request.POST
        form = ResultSlipForm(data, division=division, pairings_by_round=pbr)
        if form.is_valid():
            rs = form.save()
            if rs.pairing and rs.pairing.round_pairings:
                rs.pairing.round_pairings.update_status()
            fresh_pbr = _pairings_by_round(division)
            fresh_form = ResultSlipForm(division=division, pairings_by_round=fresh_pbr)
            context = self._form_context(
                division, fresh_form,
                success_message="Result saved. If there are any mistakes, edit the form and click save again. If everything looks correct, hit Done to close the form.",
            )
            if is_datastar(request):
                return fragment_response(
                    "tournaments/_resultslip_form.html", context, request=request,
                )
            return render(request, self.template_name, context)
        context = self._form_context(division, form)
        if is_datastar(request):
            return fragment_response(
                "tournaments/_resultslip_form.html", context, request=request,
            )
        return render(request, self.template_name, context)


class DivisionEditResultsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_edit_results.html"

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
            "active_tab": "edit_results",
            "can_edit": True,
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)

        rows = data if isinstance(data, list) else data.get("results", [])
        entrant_ids = set(division.entrants.values_list("pk", flat=True))
        validated, errors = parse_rows(ResultSlipDTO, rows, entrant_ids)
        if errors:
            return JsonResponse({"errors": errors}, status=400)

        pairing_lookup = division.pairings_by_round_pair()

        # Track which RoundPairings are affected for status updates.
        affected_rounds = set(
            division.result_slips.values_list("round", flat=True)
        )

        division.result_slips.all().delete()
        for slip in validated:
            db_kwargs = slip.to_db_kwargs()
            # Match to Pairing object by round + entrant pair.
            key = (db_kwargs["round"], frozenset({db_kwargs["winner_id"], db_kwargs["loser_id"]}))
            pairing_obj = pairing_lookup.get(key)
            ResultSlip.objects.create(division=division, pairing=pairing_obj, **db_kwargs)
            affected_rounds.add(db_kwargs["round"])

        # Update status for all affected rounds.
        for rp in division.round_pairings_set.filter(round__in=affected_rounds):
            rp.update_status()

        return JsonResponse({"ok": True})


def _read_request_data(request):
    """Parse request body as JSON (or datastar signals). Returns (data, error_response)."""
    if is_datastar(request):
        return read_signals(request) or {}, None
    try:
        return json.loads(request.body), None
    except json.JSONDecodeError:
        return None, JsonResponse({"error": "Invalid JSON."}, status=400)


def _simulation_response(request, division):
    if is_datastar(request):
        context = _build_pairings_context(division)
        return fragment_response(
            "tournaments/_round_tab_content.html", context, request=request
        )
    return JsonResponse({"ok": True})


class SimulateMatchView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Simulate a single match for a test division and create a result slip."""

    def post(self, request, pk):
        division = self.get_division()
        if not division.is_test:
            return JsonResponse(
                {"error": "Simulation is only available for test divisions."},
                status=403,
            )

        data, error = _read_request_data(request)
        if error:
            return error

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

        slip = simulate_match(division, round_num, first_entrant, second_entrant)
        if slip.pairing and slip.pairing.round_pairings:
            slip.pairing.round_pairings.update_status()

        return _simulation_response(request, division)


class SimulateRoundView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Simulate all remaining matches in a round for a test division."""

    def post(self, request, pk):
        division = self.get_division()
        if not division.is_test:
            return JsonResponse(
                {"error": "Simulation is only available for test divisions."},
                status=403,
            )

        data, error = _read_request_data(request)
        if error:
            return error

        round_num = data.get("round")
        if not round_num:
            return JsonResponse({"error": "Missing required fields."}, status=400)

        simulate_round(division, round_num)
        return _simulation_response(request, division)
