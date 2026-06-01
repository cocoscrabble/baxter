import json
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import models
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils.text import slugify
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
from .fixed_pairings import (
    add_fixed_pairing,
    remove_fixed_pairing,
    remove_fixed_pairings,
)
from .match_simulation import simulate_match, simulate_round
from .grids import EntrantsGrid, FixedPairingsGrid, FixedTablesGrid, ResultsGrid
from .models import EDIT_SCOPES, Division, DivisionSettings, Pairing, Player, RoundPairings, Tournament
from editgrid.concurrency import check_conflict
from editgrid.models import EditVersion
from editgrid.views import BaseEditGridView, EditPresenceBaseView
from .player_sync import import_players
from users.models import User
from .generate_pairings import regenerate_pairings
from .pairing.base import PairingData, standings_after_round
from .pairing.pair import STRATEGY_TYPES
from .pairings_view import PairingsPresenter, PublishedPairingsPresenter
from .scorecards import ScorecardSpec, make_rounds, render_scorecards

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


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


class IsAdminMixin(UserPassesTestMixin):
    """Allow only Django superusers or users with the Admin role.

    With the default ``raise_exception = False``, Django redirects anonymous
    users to the login page but returns 403 for an authenticated non-admin.
    """

    def test_func(self):
        user = self.request.user
        return user.is_authenticated and (
            user.is_superuser or user.role == User.Role.ADMIN
        )


def _ensure_visible_division(division, user):
    """Raise Http404 if this is a test division the user is not allowed to see."""
    if division.is_test and not division.tournament.can_edit(user):
        raise Http404


def edit_key(division, scope):
    """Opaque editgrid key identifying one division's editable grid.

    The editgrid app (version tokens, presence) is domain-agnostic and keys
    everything on this string; Baxter composes it from the division and scope.
    """
    return f"division:{division.pk}:{scope}"


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
    template_name = "tournaments/division_confirm_delete.html"

    def get(self, request, pk):
        division = self.get_division()
        return render(request, self.template_name, {"division": division})

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




def _pairings_body_response(request, division, *, select_round=None, error=None):
    """Datastar: re-render the pairings body, optionally focused on ``select_round``.

    Falls back to a flash + redirect for non-Datastar (no-JS) submissions, which
    keeps the existing full-page behaviour and its tests intact.
    """
    if is_datastar(request):
        presenter = PairingsPresenter(division)
        if select_round is not None:
            presenter.select(select_round)
        context = {
            "division": division,
            "can_edit": True,
            "active_tab": "pairings",
        }
        context.update(presenter.as_context())
        context.update(_editor_pairings_context(division, presenter))
        if error:
            context["fixed_error"] = error
        return fragment_response(
            "tournaments/_pairings_body.html", context, request=request
        )
    if error:
        messages.error(request, error)
    return redirect("division_pairings", pk=division.pk)


class PublishPairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Publish every draft round at once."""

    def post(self, request, pk):
        division = self.get_division()
        division.round_pairings_set.filter(
            status=RoundPairings.DRAFT
        ).update(status=RoundPairings.PUBLISHED)
        return _pairings_body_response(request, division)


class PublishRoundView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Publish a single draft round and live-swap the pairings body."""

    def post(self, request, pk):
        division = self.get_division()
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        round_number = int(data["round"])
        division.round_pairings_set.filter(
            round=round_number, status=RoundPairings.DRAFT
        ).update(status=RoundPairings.PUBLISHED)
        return _pairings_body_response(request, division, select_round=round_number)


class AddFixedPairingView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        round_number = int(data["round"])
        entrant1_id = int(data["entrant1"])
        entrant2_id = int(data["entrant2"])
        _, error = add_fixed_pairing(division, round_number, entrant1_id, entrant2_id)
        return _pairings_body_response(request, division, select_round=round_number, error=error)


class RemoveFixedPairingView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Delete a single fixed pairing inline and live-regenerate its round."""

    def post(self, request, pk):
        division = self.get_division()
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        fp_id = int(data["fp_id"])
        round_number = int(data["round"])
        _, error = remove_fixed_pairing(division, fp_id)
        return _pairings_body_response(request, division, select_round=round_number, error=error)


class RemoveFixedPairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, pk):
        division = self.get_division()
        keep_ids = set(request.POST.getlist("keep"))
        error = remove_fixed_pairings(division, keep_ids)
        if error:
            messages.error(request, error)
        return redirect("division_pairings", pk=pk)


def _entrants_for_editing(division):
    return list(
        division.entrants.select_related("player").order_by("player__name")
    )


def _editor_pairings_context(division, presenter):
    """Controls/edit context shared by the full page and every fragment endpoint.

    Keeps the publish control and the inline fixed-pairing editor consistent
    whichever endpoint rendered the ``_pairings_body`` swap unit.
    """
    context = {"entrants": _entrants_for_editing(division)}
    msg = presenter.waiting_message
    if msg:
        context["waiting_message"] = msg
    return context


def _autogenerate_pairable_rounds(division):
    """Generate pairings for any pairable round that has none yet.

    Makes 'pairable' imply 'has draft pairings', so the pairings tab only needs
    a Publish action rather than a manual Generate step. Regeneration only
    touches draft rounds and is idempotent for the deterministic strategies, so
    running it lazily on render is safe; it is a no-op once every pairable round
    has pairings.
    """
    if PairingsPresenter(division).rounds_needing_generation:
        regenerate_pairings(division)


class DivisionPairingsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_pairings.html"
    context_object_name = "division"
    active_tab = "pairings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if context["can_edit"]:
            _autogenerate_pairable_rounds(self.object)
        presenter = PairingsPresenter(self.object)
        context.update(presenter.as_context())
        if context["can_edit"]:
            context.update(_editor_pairings_context(self.object, presenter))
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
        if context.get("can_edit"):
            _autogenerate_pairable_rounds(self.object)
        presenter = PairingsPresenter(self.object).select(round)
        context.update(presenter.as_context())
        if context.get("can_edit"):
            context.update(_editor_pairings_context(self.object, presenter))
        if is_datastar(request):
            return fragment_response(
                "tournaments/_pairings_body.html", context, request=request
            )
        return self.render_to_response(context)


class PublishedPairingsView(VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/published_pairings.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(PublishedPairingsPresenter(self.object).as_context())
        return context


class DivisionScorecardsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    """Scorecards landing page with a generate-and-download action."""

    model = Division
    template_name = "tournaments/division_scorecards.html"
    context_object_name = "division"
    active_tab = "scorecards"


class DivisionScorecardsDownloadView(VisibleDivisionMixin, DetailView):
    """Download a .docx with a printable scorecard for every entrant."""

    model = Division
    context_object_name = "division"

    def render_to_response(self, context, **kwargs):
        division = self.object
        tournament = division.tournament
        rounds = make_rounds(division.configured_round_numbers())
        qr_url = self.request.build_absolute_uri(
            reverse("published_pairings", args=[division.pk])
        )
        opponents = self._opponents_by_entrant(division)
        specs = [
            ScorecardSpec(
                tournament_name=tournament.name,
                tournament_date=tournament.start_date.strftime("%B %-d, %Y"),
                player_name=entrant.name,
                rounds=rounds,
                opponents=opponents.get(entrant.pk, {}),
                qr_url=qr_url,
            )
            for entrant in division.entrants.all()
        ]

        response = HttpResponse(
            render_scorecards(specs), content_type=DOCX_CONTENT_TYPE
        )
        filename = slugify(f"{tournament.name}-{division.name}-scorecards") + ".docx"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @staticmethod
    def _opponents_by_entrant(division):
        """Map each entrant id to a {round: opponent name} dict from pairings."""
        pairings = division.pairings.select_related(
            "first__player", "second__player"
        )
        opponents = defaultdict(dict)
        for p in pairings:
            if p.first_id == p.second_id:
                continue  # bye — no opponent to prefill
            opponents[p.first_id][p.round] = p.second.name
            opponents[p.second_id][p.round] = p.first.name
        return opponents


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


class DivisionEditGridView(LoginRequiredMixin, CanEditDivisionMixin, BaseEditGridView):
    """Baxter base for editable grids: division-scoped and permission-gated.

    Concrete grids subclass this and set ``grid`` (an EditGrid) and
    ``active_tab`` as class attributes."""

    active_tab = ""

    def get_parent(self):
        return self.get_division()

    def grid_key(self, division):
        return edit_key(division, self.grid.scope)

    def presence_url(self, division):
        return reverse(
            "edit_presence", kwargs={"pk": division.pk, "scope": self.grid.scope}
        )

    def get_context_data(self, division):
        context = super().get_context_data(division)
        context.update(
            {"division": division, "active_tab": self.active_tab, "can_edit": True}
        )
        return context


class DivisionEntrantsEditView(DivisionEditGridView):
    grid = EntrantsGrid()
    active_tab = "edit_entrants"


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


class PlayerImportView(LoginRequiredMixin, IsAdminMixin, View):
    """Admin-only page to upsert the global player roster from a JSON upload.

    Pairs with the ``export_players`` management command: export on the source
    machine, upload the file here. Players are matched on player_number, so
    re-uploading is safe and only adds/updates rows.
    """

    template_name = "tournaments/player_import.html"

    def get(self, request):
        return render(request, self.template_name, {
            "player_count": Player.objects.count(),
        })

    def post(self, request):
        uploaded = request.FILES.get("players_file")
        if not uploaded:
            messages.error(request, "No file uploaded.")
            return redirect("player_import")

        result, errors = import_players(uploaded.read())
        if errors:
            for error in errors[:25]:
                messages.error(request, error)
            if len(errors) > 25:
                messages.error(request, f"... and {len(errors) - 25} more.")
        else:
            messages.success(
                request,
                f"Imported {result['total']} player(s): {result['added']} added, "
                f"{result['updated']} updated, {result['unchanged']} unchanged."
            )
        return redirect("player_import")


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


class DivisionFixedPairingsEditView(DivisionEditGridView):
    grid = FixedPairingsGrid()
    active_tab = "fixed_pairings"


class DivisionFixedTablesEditView(DivisionEditGridView):
    grid = FixedTablesGrid()
    active_tab = "fixed_tables"


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
            "edit_version": EditVersion.version_for(edit_key(division, "board_table_map")),
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
        with check_conflict(edit_key(division, "board_table_map"), data.get("_version")) as guard:
            if guard.conflict:
                return guard.conflict
            settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
            settings_obj.board_table_map = validated
            settings_obj.save(update_fields=["board_table_map"])
        return guard.response


class EditPresenceView(LoginRequiredMixin, CanEditDivisionMixin, EditPresenceBaseView):
    """Baxter binding of the generic editgrid presence endpoint: gates on
    division-edit permission and derives the key from the division + scope."""

    def get_key(self):
        scope = self.kwargs["scope"]
        if scope not in EDIT_SCOPES:
            return None
        return edit_key(self.get_division(), scope)


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


class DivisionEditResultsView(DivisionEditGridView):
    grid = ResultsGrid()
    active_tab = "edit_results"


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
        context = PairingsPresenter(division).as_context()
        return fragment_response(
            "tournaments/_round_tab_content.html", context, request=request
        )
    return JsonResponse({"ok": True})


def _require_published_round(division, round_num):
    """Return an error JsonResponse if the round's pairings aren't committed.

    Simulation records results against pairings, so it's only safe once a round
    is published: a draft (pairable) round can still be regenerated, and a round
    with no RoundPairings at all has nothing to attach results to — either way
    we'd be left with results referencing pairings that may not exist. Returns
    None when the round is published/in-progress/finished.
    """
    rp = division.round_pairings_set.filter(round=round_num).first()
    if rp is None or rp.status == RoundPairings.DRAFT:
        return JsonResponse(
            {"error": "Round must be published before results can be simulated."},
            status=409,
        )
    return None


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

        error = _require_published_round(division, round_num)
        if error:
            return error

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

        error = _require_published_round(division, round_num)
        if error:
            return error

        simulate_round(division, round_num)
        return _simulation_response(request, division)
