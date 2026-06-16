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
    TournamentForm,
)
from .fixed_pairings import (
    add_fixed_pairing,
    remove_fixed_pairing,
    remove_fixed_pairings,
)
from .match_simulation import simulate_match, simulate_round
from .grids import BoardTableMapGrid, EntrantsGrid, FixedPairingsGrid, FixedTablesGrid, ResultsGrid
from .models import EDIT_SCOPES, Division, DivisionSettings, Pairing, Player, ResultSlip, RoundPairings, Tournament
from editgrid.concurrency import check_conflict
from editgrid.models import EditVersion
from editgrid.views import BaseEditGridView, EditPresenceBaseView, build_grid_context
from .pairing.round_pairing import (
    blocks_to_round_pairings,
    default_block_rounds,
    round_pairings_to_blocks,
)
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
        Division.objects.create(tournament=self.object, name="Division 1")
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


class DivisionRenameView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Rename a division inline from the tournament detail page (datastar)."""

    def post(self, request, pk):
        division = self.get_division()
        tournament = division.tournament
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        name = (data.get("name") or "").strip()
        error = None
        if not name:
            error = "Division name cannot be empty."
        elif (
            Division.all_objects.filter(tournament=tournament, name=name)
            .exclude(pk=division.pk)
            .exists()
        ):
            error = f"A division named “{name}” already exists."
        else:
            division.name = name
            division.save(update_fields=["name"])

        if is_datastar(request):
            context = {"tournament": tournament, "can_edit": True, "rename_error": error}
            context.update(tournament.division_buckets())
            # Reset the inline-edit signals so the swapped-in table shows the
            # display state regardless of how the morph re-applies data-signals.
            return fragment_response(
                "tournaments/_division_management.html", context, request=request,
                signals={"renamingPk": 0, "renameName": ""},
            )
        if error:
            messages.error(request, error)
        return redirect("tournament_detail", pk=tournament.pk)


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
        opponents, starts = self._prefills_by_entrant(division)
        specs = [
            ScorecardSpec(
                tournament_name=tournament.name,
                tournament_date=tournament.start_date.strftime("%B %-d, %Y"),
                player_name=entrant.name,
                rounds=rounds,
                opponents=opponents.get(entrant.pk, {}),
                starts=starts.get(entrant.pk, {}),
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
    def _prefills_by_entrant(division):
        """Map each entrant id to its {round: opponent name} and
        {round: "1st"/"2nd"} prefills, drawn from the division's pairings."""
        pairings = division.pairings.select_related(
            "first__player", "second__player"
        )
        opponents = defaultdict(dict)
        starts = defaultdict(dict)
        for p in pairings:
            if p.first_id == p.second_id:
                continue  # bye — nothing to prefill
            opponents[p.first_id][p.round] = p.second.name
            opponents[p.second_id][p.round] = p.first.name
            starts[p.first_id][p.round] = "1st"
            starts[p.second_id][p.round] = "2nd"
        return opponents, starts


class DivisionSettingsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Placeholder for future per-division configuration. Round pairings, which
    used to live here, now have their own tab."""

    template_name = "tournaments/division_settings_edit.html"

    def get(self, request, pk):
        division = self.get_division()
        return render(request, self.template_name, {
            "division": division,
            "active_tab": "settings",
            "can_edit": True,
        })


def _validate_blocks(raw):
    """Validate raw pairing-block dicts. Returns ``(blocks, errors)``."""
    valid = {str(s) for s in STRATEGY_TYPES}
    blocks, errors = [], []
    for i, b in enumerate(raw):
        pairing = b.get("pairing")
        if pairing not in valid:
            errors.append(f"Row {i + 1}: choose a pairing type.")
            continue
        try:
            rounds = int(b.get("rounds"))
            pair_from = int(b.get("pair_from") or 1)
        except (TypeError, ValueError):
            errors.append(f"Row {i + 1}: rounds and pair-from must be whole numbers.")
            continue
        if rounds < 1:
            errors.append(f"Row {i + 1}: rounds must be at least 1.")
            continue
        blocks.append({"pairing": pairing, "rounds": rounds, "pair_from": pair_from})
    return blocks, errors


class DivisionRoundPairingsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Block-based round-pairings editor. Blocks are the source of truth; the
    per-round ``round_pairings`` the engine reads are derived from them."""

    template_name = "tournaments/division_round_pairings_edit.html"

    def get(self, request, pk):
        division = self.get_division()
        settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
        blocks = settings_obj.pairing_blocks
        # Seed the editor from an existing schedule if blocks were never saved.
        if not blocks and settings_obj.round_pairings:
            blocks = round_pairings_to_blocks(settings_obj.round_pairings)
        preview = [rp.to_dict() for rp in blocks_to_round_pairings(blocks)]
        key = edit_key(division, "round_pairings")
        return render(request, self.template_name, {
            "division": division,
            "blocks_json": json.dumps(blocks),
            "preview_json": json.dumps(preview),
            "default_rounds_json": json.dumps(default_block_rounds(division.entrants.count())),
            "strategy_types_json": json.dumps([str(s) for s in STRATEGY_TYPES]),
            "edit_version": EditVersion.version_for(key),
            "presence_url": reverse(
                "edit_presence", kwargs={"pk": division.pk, "scope": "round_pairings"}
            ),
            "preview_url": reverse("division_round_pairings_preview", kwargs={"pk": division.pk}),
            "active_tab": "round_pairings",
            "can_edit": True,
        })

    def post(self, request, pk):
        division = self.get_division()
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)
        blocks, errors = _validate_blocks(data.get("blocks", []))
        if errors:
            return JsonResponse({"errors": errors}, status=400)
        round_pairings = [rp.to_dict() for rp in blocks_to_round_pairings(blocks)]
        with check_conflict(edit_key(division, "round_pairings"), data.get("_version")) as guard:
            if guard.conflict:
                return guard.conflict
            settings_obj, _ = DivisionSettings.objects.get_or_create(division=division)
            settings_obj.pairing_blocks = blocks
            settings_obj.round_pairings = round_pairings
            settings_obj.save(update_fields=["pairing_blocks", "round_pairings"])
        return guard.response


class DivisionRoundPairingsPreviewView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Expand blocks into the per-round list without saving — drives the live
    preview table."""

    def post(self, request, pk):
        self.get_division()  # permission gate
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"errors": ["Invalid JSON."]}, status=400)
        # Lenient: expand whatever blocks are valid so partial edits still
        # preview (a half-filled row just doesn't contribute rounds yet).
        blocks, _errors = _validate_blocks(data.get("blocks", []))
        rows = [rp.to_dict() for rp in blocks_to_round_pairings(blocks)]
        return JsonResponse({"ok": True, "rows": rows})


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
            "rating": player.rating,
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


class DivisionFixturesEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Combined page editing Fixed Pairings and Fixed Tables side by side.

    GET-only: it renders two grid contexts. Each grid saves and heartbeats to
    its own existing endpoint, so the combined page never handles a POST.
    """

    template_name = "tournaments/division_fixtures_edit.html"

    GRIDS = [
        (FixedPairingsGrid(), "division_fixed_pairings", "pairings_grid"),
        (FixedTablesGrid(), "division_fixed_tables", "tables_grid"),
    ]

    def get(self, request, pk):
        division = self.get_division()
        context = {"division": division, "active_tab": "fixtures", "can_edit": True}
        for grid, route, ctx_name in self.GRIDS:
            context[ctx_name] = build_grid_context(
                grid,
                division,
                key=edit_key(division, grid.scope),
                presence_url=reverse(
                    "edit_presence", kwargs={"pk": division.pk, "scope": grid.scope}
                ),
                save_url=reverse(route, kwargs={"pk": division.pk}),
            )
        return render(request, self.template_name, context)


class DivisionBoardTableMapEditView(DivisionEditGridView):
    grid = BoardTableMapGrid()
    active_tab = "board_tables"

    def get_context_data(self, division):
        context = super().get_context_data(division)
        context["default_board_count"] = (division.entrants.count() + 1) // 2
        return context


class EditPresenceView(LoginRequiredMixin, CanEditDivisionMixin, EditPresenceBaseView):
    """Baxter binding of the generic editgrid presence endpoint: gates on
    division-edit permission and derives the key from the division + scope."""

    def get_key(self):
        scope = self.kwargs["scope"]
        if scope not in EDIT_SCOPES:
            return None
        return edit_key(self.get_division(), scope)


def _pairing_tuple(p):
    return (p.pk, p.first_id, p.first.player.name, p.second_id, p.second.player.name)


def _pairings_by_round(division, include_pairing=None):
    """Build pairings data grouped by round for the ResultSlipForm.

    Returns {round_num: [(pairing_pk, first_pk, first_name, second_pk, second_name), ...]}
    Only includes rounds with published/in_progress status and pairings without
    results. ``include_pairing`` forces a specific pairing into the result even if
    it already has one — used when editing that pairing's existing result.
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
            pairing_list.append(_pairing_tuple(p))
        if pairing_list:
            result[rp.round] = pairing_list
    if include_pairing is not None:
        bucket = result.setdefault(include_pairing.round, [])
        if all(entry[0] != include_pairing.pk for entry in bucket):
            bucket.append(_pairing_tuple(include_pairing))
    return result


_BLANK_RESULT_SIGNALS = {
    "round": "", "pairing": "", "winner": "",
    "winner_score": "", "loser_score": "", "winner_started": False,
}


def _result_initial(result):
    return {
        "round": result.round,
        "pairing": result.pairing_id,
        "winner": result.winner_id,
        "winner_score": result.winner_score,
        "loser_score": result.loser_score,
        "winner_started": result.winner_started,
    }


def _result_signals(result):
    return {
        "round": str(result.round),
        "pairing": str(result.pairing_id),
        "winner": str(result.winner_id),
        "winner_score": result.winner_score,
        "loser_score": result.loser_score,
        "winner_started": result.winner_started,
    }


def _result_summary(result):
    return (
        f"R{result.round}: {result.winner_name} {result.winner_score}"
        f"–{result.loser_score} {result.loser_name}"
    )


class ResultSlipCreateView(View):
    template_name = "tournaments/resultslip_form.html"

    def get_division(self):
        division = get_object_or_404(Division, pk=self.kwargs["pk"])
        _ensure_visible_division(division, self.request.user)
        return division

    def get_result(self, division):
        """Return the ResultSlip being edited, or None when adding a new one."""
        result_pk = self.kwargs.get("result_pk")
        if result_pk is None:
            return None
        return get_object_or_404(
            ResultSlip.objects.select_related(
                "pairing__first__player",
                "pairing__second__player",
                "pairing__round_pairings",
                "winner__player",
                "loser__player",
            ),
            pk=result_pk,
            division=division,
        )

    def _form_context(self, division, form, *, editing=False, result=None, saved=None):
        pbr = form._pairings_by_round
        # Build JSON-safe pairings data for datastar client-side filtering.
        pairings_json = {}
        for r, pairing_list in pbr.items():
            pairings_json[str(r)] = [
                {"pk": p_pk, "first_pk": f_pk, "first_name": f_name,
                 "second_pk": s_pk, "second_name": s_name}
                for p_pk, f_pk, f_name, s_pk, s_name in pairing_list
            ]
        if editing and result is not None:
            form_action = reverse("resultslip_edit", args=[division.pk, result.pk])
        else:
            form_action = reverse("resultslip_create", args=[division.pk])
        context = {
            "form": form,
            "division": division,
            "pairings_json": json.dumps(pairings_json),
            "active_tab": "add_result",
            "can_edit": division.tournament.can_edit(self.request.user),
            "editing": editing,
            "form_action": form_action,
        }
        if saved is not None:
            context["saved_result"] = {
                "summary": _result_summary(saved),
                "edit_url": reverse("resultslip_edit", args=[division.pk, saved.pk]),
            }
        return context

    def _render(self, request, context, signals=None):
        if is_datastar(request):
            return fragment_response(
                "tournaments/_resultslip_form.html", context,
                request=request, signals=signals,
            )
        return render(request, self.template_name, context)

    def get(self, request, *args, **kwargs):
        division = self.get_division()
        result = self.get_result(division)
        if result is not None:
            pbr = _pairings_by_round(division, include_pairing=result.pairing)
            form = ResultSlipForm(
                division=division, pairings_by_round=pbr,
                instance=result, initial=_result_initial(result),
            )
            context = self._form_context(division, form, editing=True, result=result)
            return self._render(request, context, signals=_result_signals(result))
        pbr = _pairings_by_round(division)
        form = ResultSlipForm(division=division, pairings_by_round=pbr)
        context = self._form_context(division, form)
        return self._render(request, context, signals=_BLANK_RESULT_SIGNALS)

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        result = self.get_result(division)
        include = result.pairing if result is not None and result.pairing_id else None
        pbr = _pairings_by_round(division, include_pairing=include)
        if is_datastar(request):
            data = read_signals(request) or {}
        else:
            data = request.POST
        form = ResultSlipForm(
            data, division=division, pairings_by_round=pbr, instance=result,
        )
        if form.is_valid():
            rs = form.save()
            if rs.pairing and rs.pairing.round_pairings:
                rs.pairing.round_pairings.update_status()
            # Reset to a blank form for the next entry, surfacing the saved
            # result with an Edit button to correct it if needed.
            fresh_pbr = _pairings_by_round(division)
            fresh_form = ResultSlipForm(division=division, pairings_by_round=fresh_pbr)
            context = self._form_context(division, fresh_form, saved=rs)
            return self._render(request, context, signals=_BLANK_RESULT_SIGNALS)
        context = self._form_context(
            division, form, editing=result is not None, result=result,
        )
        return self._render(request, context)


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
