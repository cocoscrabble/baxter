import json
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
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
    FakeTournamentForm,
    ResultSlipForm,
    TournamentForm,
)
from .fake_tournament import create_fake_tournament, default_fake_tournament_name
from .fixed_pairings import (
    add_fixed_pairing,
    remove_fixed_pairing,
    remove_fixed_pairings,
)
from .match_simulation import simulate_match, simulate_round
from .commands import (
    add_fixed_pairing_cmd,
    add_result,
    bulk_import_entrants,
    create_division,
    create_tournament,
    edit_result,
    delete_division,
    delete_tournament,
    publish_all_rounds,
    publish_round,
    remove_fixed_pairing_cmd,
    remove_fixed_pairings_cmd,
    rename_division,
    restore_division,
    save_settings,
    simulate_match_cmd,
    simulate_round_cmd,
    unpublish_round,
    update_tournament,
)
from .grids import BoardTableMapGrid, EntrantsGrid, FixedPairingsGrid, FixedTablesGrid, ResultsGrid
from .models import (
    EDIT_SCOPES,
    Division,
    DivisionSettings,
    DivisionSlugAlias,
    Pairing,
    Player,
    ResultSlip,
    RoundPairings,
    Tournament,
    TournamentSlugAlias,
)
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
from .generate_pairings import publish_rounds, regenerate_pairings, unpublish_rounds
from .pairing.base import PairingData, PairingError, standings_after_round
from .pairing.pair import STRATEGY_TYPES
from .pairings_view import PairingsPresenter, PublishedPairingsPresenter
from .scorecards import ScorecardResult, ScorecardSpec, make_rounds, render_scorecards
from .results_export import ResultRow, render_results_csv

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class TournamentListView(ListView):
    model = Tournament
    template_name = "tournaments/tournament_list.html"
    context_object_name = "tournaments"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Drives the per-row delete link, shown only for fake tournaments the
        # current user is allowed to remove.
        user = self.request.user
        for tournament in context["tournaments"]:
            tournament.user_can_delete = tournament.can_delete(user)
        return context


def _tournament_payload(form):
    return {
        "name": form.cleaned_data["name"],
        "location": form.cleaned_data["location"],
        "start_date": form.cleaned_data["start_date"].isoformat(),
        "editors": [
            u.username for u in form.cleaned_data.get("editor_usernames", [])
        ],
    }


class TournamentCreateView(LoginRequiredMixin, CreateView):
    model = Tournament
    form_class = TournamentForm
    template_name = "tournaments/tournament_form.html"

    def form_valid(self, form):
        self.object = create_tournament(
            None, self.request.user, _tournament_payload(form)
        )
        return redirect(self.get_success_url())

    def get_success_url(self):
        return self.object.get_absolute_url()


class FakeTournamentCreateView(LoginRequiredMixin, View):
    """Generate a fully-simulated test tournament from random players."""

    template_name = "tournaments/fake_tournament_form.html"

    def get(self, request):
        form = FakeTournamentForm(
            initial={"name": default_fake_tournament_name()}
        )
        return render(request, self.template_name, {"form": form})

    def post(self, request):
        form = FakeTournamentForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})
        num_players = form.cleaned_data["num_players"]
        num_rounds = form.cleaned_data["num_rounds"]
        division = create_fake_tournament(
            request.user, num_players, num_rounds, name=form.cleaned_data["name"]
        )
        messages.success(
            request,
            f"Created test tournament “{division.tournament.name}” "
            f"with {num_players} players over {num_rounds} rounds.",
        )
        return redirect("division_pair_rounds", **division.slug_kwargs())


def _resolve_tournament(slug):
    """``(tournament, is_canonical)``: resolve a tournament by its current slug,
    then by a :class:`TournamentSlugAlias`. ``(None, False)`` if nothing matches."""
    tournament = Tournament.objects.filter(slug=slug).first()
    if tournament is not None:
        return tournament, True
    alias = (
        TournamentSlugAlias.objects.select_related("tournament").filter(slug=slug).first()
    )
    if alias is not None:
        return alias.tournament, False
    return None, False


def _resolve_division(tournament_slug, division_slug, manager=None):
    """``(division, is_canonical)`` from the two URL slugs, following tournament and
    division slug aliases. ``is_canonical`` is False when either slug was an alias
    (caller should 301 to the canonical URL). Raises ``Http404`` if unresolved."""
    manager = (manager or Division.objects).select_related("tournament")
    # Fast path: both slugs canonical — one query, with the tournament prefetched
    # for the can_edit/redirect lookups every division view makes.
    division = manager.filter(
        tournament__slug=tournament_slug, slug=division_slug
    ).first()
    if division is not None:
        return division, True
    # Otherwise one or both slugs is an alias; resolve each namespace in turn.
    tournament, _ = _resolve_tournament(tournament_slug)
    if tournament is None:
        raise Http404
    division = manager.filter(tournament=tournament, slug=division_slug).first()
    if division is None:
        alias = DivisionSlugAlias.objects.filter(
            tournament=tournament, slug=division_slug
        ).first()
        if alias is not None:
            division = manager.filter(pk=alias.division_id).first()
    if division is None:
        raise Http404
    return division, False


def _redirect_to_canonical(request, kwargs, obj):
    """301 to the current view with ``obj``'s canonical slug(s), preserving the
    other kwargs and the query string. Used when an old (aliased) slug is requested."""
    new_kwargs = {**kwargs, **obj.slug_kwargs()}
    url = reverse(request.resolver_match.url_name, kwargs=new_kwargs)
    query = request.META.get("QUERY_STRING")
    if query:
        url = f"{url}?{query}"
    return redirect(url, permanent=True)


class TournamentURLMixin:
    """Resolve a tournament from the ``tournament_slug`` URL kwarg (following slug
    aliases, 301-ing an old slug to the canonical one) and expose it as the view's
    object."""

    def dispatch(self, request, *args, **kwargs):
        self.tournament, canonical = _resolve_tournament(kwargs["tournament_slug"])
        if self.tournament is None:
            raise Http404
        if not canonical:
            return _redirect_to_canonical(request, kwargs, self.tournament)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        return self.tournament


class CanEditTournamentMixin(UserPassesTestMixin):
    """Mixin that checks if user can edit the tournament."""

    def test_func(self):
        return self.get_object().can_edit(self.request.user)


class DivisionURLMixin:
    """Resolve a division from the ``tournament_slug``/``division_slug`` URL kwargs
    (following slug aliases, 301-ing old slugs to canonical) and expose it via
    ``get_division`` / ``get_object``. Set ``division_manager`` to
    ``Division.all_objects`` on views that act on soft-deleted divisions."""

    division_manager = None

    def dispatch(self, request, *args, **kwargs):
        self.division, canonical = _resolve_division(
            kwargs["tournament_slug"], kwargs["division_slug"], self.division_manager
        )
        if not canonical:
            return _redirect_to_canonical(request, kwargs, self.division)
        return super().dispatch(request, *args, **kwargs)

    def get_division(self):
        return self.division

    def get_object(self, queryset=None):
        return self.division


class CanEditDivisionMixin(DivisionURLMixin, UserPassesTestMixin):
    """Mixin that checks if user can edit the division's tournament."""

    def test_func(self):
        return self.division.tournament.can_edit(self.request.user)


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


class VisibleDivisionMixin(DivisionURLMixin):
    """Resolve the division (via :class:`DivisionURLMixin`) and 404 for test
    divisions the user is not allowed to see."""

    def get_object(self, queryset=None):
        _ensure_visible_division(self.division, self.request.user)
        return self.division


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


class TournamentDetailView(TournamentURLMixin, DetailView):
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


class TournamentUpdateView(TournamentURLMixin, LoginRequiredMixin, CanEditTournamentMixin, UpdateView):
    model = Tournament
    form_class = TournamentForm
    template_name = "tournaments/tournament_form.html"
    context_object_name = "tournament"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(self.object.division_buckets())
        return context

    def form_valid(self, form):
        update_tournament(self.object, self.request.user, _tournament_payload(form))
        return redirect(self.get_success_url())

    def get_success_url(self):
        return self.object.get_absolute_url()


class CanDeleteTournamentMixin(UserPassesTestMixin):
    """Mixin that checks whether the user may delete the tournament."""

    def test_func(self):
        tournament = self.get_object()
        return tournament.can_delete(self.request.user)


class TournamentDeleteView(TournamentURLMixin, LoginRequiredMixin, CanDeleteTournamentMixin, DeleteView):
    model = Tournament
    template_name = "tournaments/tournament_confirm_delete.html"
    context_object_name = "tournament"
    success_url = reverse_lazy("tournament_list")

    def post(self, request, *args, **kwargs):
        delete_tournament(self.get_object(), request.user, {})
        return redirect(self.success_url)


class DivisionCreateView(LoginRequiredMixin, View):
    def post(self, request, tournament_slug):
        tournament = get_object_or_404(Tournament, slug=tournament_slug)
        if not tournament.can_edit(request.user):
            raise PermissionDenied
        name = request.POST.get("name", "").strip()
        is_test = request.POST.get("is_test") == "1"
        if name:
            # unique_together (tournament, name) spans soft-deleted rows, so a
            # get_or_create via the active-only manager would 500 with an
            # IntegrityError when the name belongs to a soft-deleted division.
            existing = Division.all_objects.filter(
                tournament=tournament, name=name
            ).first()
            if existing is not None and existing.is_deleted:
                messages.error(
                    request,
                    f"“{name}” belongs to a deleted division — restore or "
                    "rename it before reusing the name.",
                )
            elif existing is None:
                create_division(
                    tournament, request.user, {"name": name, "is_test": is_test}
                )
            # An existing active division with this name is a silent no-op, as
            # before.
        return redirect("tournament_detail", **tournament.slug_kwargs())


class DivisionDeleteView(LoginRequiredMixin, CanEditDivisionMixin, View):
    template_name = "tournaments/division_confirm_delete.html"

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {"division": self.division})

    def post(self, request, *args, **kwargs):
        division = self.division
        delete_division(division.tournament, request.user, {"name": division.name})
        return redirect("tournament_detail", tournament_slug=division.tournament.slug)


class DivisionRestoreView(LoginRequiredMixin, CanEditDivisionMixin, View):
    division_manager = Division.all_objects

    def post(self, request, *args, **kwargs):
        division = self.division
        restore_division(division.tournament, request.user, {"name": division.name})
        return redirect("tournament_detail", tournament_slug=division.tournament.slug)


class DivisionRenameView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Rename a division inline from the tournament detail page (datastar)."""

    def post(self, request, *args, **kwargs):
        division = self.division
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
            rename_division(
                tournament, request.user,
                {"old_name": division.name, "new_name": name},
            )

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
        return redirect("tournament_detail", **tournament.slug_kwargs())


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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["result_slips"] = (
            self.object.result_slips
            .select_related("winner__player", "loser__player")
            .order_by("-created_at")
        )
        return context


class DivisionEntrantsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_entrants.html"
    context_object_name = "division"
    active_tab = "entrants"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Seed order: highest-rated player first (ties broken by entrant number).
        context["entrants"] = self.object.entrants.order_by(
            "-player__rating", "number"
        )
        return context


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
        # Display standings keep withdrawn players visible (marked below); their
        # games always counted in everyone's record.
        standings = standings_after_round(pd, current_round, include_dropped=True)
        # Annotate each standing with the entrant's seed number and dropped flag.
        entrants = list(division.entrants.all())
        seed_by_name = {e.name: e.number for e in entrants}
        dropped_names = {e.name for e in entrants if e.dropped}
        for p in standings:
            p.seed = seed_by_name.get(p.name)
            p.dropped = p.name in dropped_names
        context["standings"] = standings
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
            "active_tab": "pair_rounds",
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
    return redirect("division_pair_rounds", **division.slug_kwargs())


class PublishPairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Publish every draft round at once."""

    def post(self, request, *args, **kwargs):
        division = self.division
        publish_all_rounds(division.tournament, request.user, {"division": division.name})
        return _pairings_body_response(request, division)


def _read_int(data, key):
    """Parse an integer datastar/POST signal.

    Returns ``(value, None)`` on success or ``(None, JsonResponse(status=400))``
    when the key is missing or non-numeric, so a malformed signal yields a clean
    400 instead of an unhandled 500.
    """
    try:
        return int(data[key]), None
    except (KeyError, TypeError, ValueError):
        return None, JsonResponse(
            {"error": f"missing or invalid '{key}'"}, status=400
        )


class PublishRoundView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Publish a single draft round and live-swap the pairings body."""

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        round_number, error = _read_int(data, "round")
        if error:
            return error
        publish_round(
            division.tournament, request.user,
            {"division": division.name, "round": round_number},
        )
        return _pairings_body_response(request, division, select_round=round_number)


class UnpublishRoundView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Revert a published round with no results to draft so it can be edited and
    republished, then live-swap the pairings body."""

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        round_number, error = _read_int(data, "round")
        if error:
            return error
        unpublished = unpublish_round(
            division.tournament, request.user,
            {"division": division.name, "round": round_number},
        )
        error = None if unpublished else (
            f"Round {round_number} can't be unpublished — it already has results."
        )
        return _pairings_body_response(
            request, division, select_round=round_number, error=error
        )


class AddFixedPairingView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, *args, **kwargs):
        division = self.get_division()
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        round_number, error = _read_int(data, "round")
        if error:
            return error
        entrant1_id, error = _read_int(data, "entrant1")
        if error:
            return error
        entrant2_id, error = _read_int(data, "entrant2")
        if error:
            return error
        e1 = division.entrants.filter(pk=entrant1_id).select_related("player").first()
        e2 = division.entrants.filter(pk=entrant2_id).select_related("player").first()
        error = None
        if e1 is not None and e2 is not None and e1 != e2:
            _, error = add_fixed_pairing_cmd(
                division.tournament, request.user,
                {
                    "division": division.name, "round": round_number,
                    "name1": e1.player.name, "name2": e2.player.name,
                },
            )
        return _pairings_body_response(request, division, select_round=round_number, error=error)


class RemoveFixedPairingView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Delete a single fixed pairing inline and live-regenerate its round."""

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        data = (read_signals(request) or {}) if is_datastar(request) else request.POST
        fp_id, error = _read_int(data, "fp_id")
        if error:
            return error
        round_number, error = _read_int(data, "round")
        if error:
            return error
        fp = (
            division.fixed_pairings
            .select_related("entrant1__player", "entrant2__player")
            .filter(pk=fp_id).first()
        )
        error = None
        if fp is not None:
            _, error = remove_fixed_pairing_cmd(
                division.tournament, request.user,
                {
                    "division": division.name, "round": fp.round_number,
                    "name1": fp.entrant1.player.name,
                    "name2": fp.entrant2.player.name,
                },
            )
        return _pairings_body_response(request, division, select_round=round_number, error=error)


class RemoveFixedPairingsView(LoginRequiredMixin, CanEditDivisionMixin, View):
    def post(self, request, *args, **kwargs):
        division = self.get_division()
        keep_ids = set(request.POST.getlist("keep"))
        kept = []
        for fp in division.fixed_pairings.select_related(
            "entrant1__player", "entrant2__player"
        ):
            if str(fp.pk) in keep_ids:
                n1, n2 = sorted([fp.entrant1.player.name, fp.entrant2.player.name])
                kept.append([fp.round_number, n1, n2])
        error = remove_fixed_pairings_cmd(
            division.tournament, request.user,
            {"division": division.name, "kept": kept},
        )
        if error:
            messages.error(request, error)
        return redirect("division_pair_rounds", **division.slug_kwargs())


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
    """Generate pairings for any pairable round that has none yet, returning an
    error message if generation fails (else None).

    Makes 'pairable' imply 'has draft pairings', so the pairings tab only needs
    a Publish action rather than a manual Generate step. Regeneration only
    touches draft rounds and is idempotent for the deterministic strategies, so
    running it lazily on render is safe; it is a no-op once every pairable round
    has pairings. A ``PairingError`` (e.g. a stored set of fixed pairings that
    became unsatisfiable after the field changed) is caught here — regeneration
    is atomic so the schedule is untouched — and surfaced as a banner instead of
    a 500.
    """
    if PairingsPresenter(division).rounds_needing_generation:
        try:
            regenerate_pairings(division)
        except PairingError as e:
            return str(e)
    return None


class DivisionPairingsView(LoginRequiredMixin, DivisionNavMixin, CanEditDivisionMixin, DetailView):
    """The "Pair rounds" tab — generating/publishing pairings is an organiser
    tool, so it is editor-only. Players see published pairings on the public
    "Pairings" tab (:class:`DivisionPublishedPairingsView`)."""
    model = Division
    template_name = "tournaments/division_pairings.html"
    context_object_name = "division"
    active_tab = "pair_rounds"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        pairing_error = None
        if context["can_edit"]:
            pairing_error = _autogenerate_pairable_rounds(self.object)
        presenter = PairingsPresenter(self.object)
        context.update(presenter.as_context())
        if context["can_edit"]:
            context.update(_editor_pairings_context(self.object, presenter))
        if pairing_error:
            context["pairing_error"] = pairing_error
        return context


class RoundPairingsTabView(LoginRequiredMixin, DivisionNavMixin, CanEditDivisionMixin, DetailView):
    """Datastar fragment endpoint for switching between round tabs (editor-only)."""
    model = Division
    template_name = "tournaments/division_pairings.html"
    context_object_name = "division"
    active_tab = "pair_rounds"

    def get(self, request, round, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        pairing_error = None
        if context.get("can_edit"):
            pairing_error = _autogenerate_pairable_rounds(self.object)
        presenter = PairingsPresenter(self.object).select(round)
        context.update(presenter.as_context())
        if context.get("can_edit"):
            context.update(_editor_pairings_context(self.object, presenter))
        if pairing_error:
            context["pairing_error"] = pairing_error
        if is_datastar(request):
            return fragment_response(
                "tournaments/_pairings_body.html", context, request=request
            )
        return self.render_to_response(context)


class _PublishedPairingsMixin(VisibleDivisionMixin):
    """Shared published-pairings context (the rounds + their pairings) for both the
    in-nav tab and the standalone page. The two differ only in base template."""
    model = Division
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(PublishedPairingsPresenter(self.object).as_context())
        return context


class PublishedPairingsView(_PublishedPairingsMixin, DetailView):
    """Standalone (embeddable) published-pairings page — no division nav. Linked
    from the editor tab and the scorecard QR code."""
    template_name = "tournaments/published_pairings.html"


class DivisionPublishedPairingsView(DivisionNavMixin, _PublishedPairingsMixin, DetailView):
    """Public "Pairings" tab: the same published pairings shown inside the
    division nav, visible to everyone (no editing controls)."""
    template_name = "tournaments/division_published_pairings.html"
    active_tab = "pairings"


class DivisionScorecardsView(LoginRequiredMixin, DivisionNavMixin, CanEditDivisionMixin, DetailView):
    """Scorecards landing page with a generate-and-download action (editor-only)."""

    model = Division
    template_name = "tournaments/division_scorecards.html"
    context_object_name = "division"
    active_tab = "scorecards"


class DivisionScorecardsDownloadView(LoginRequiredMixin, CanEditDivisionMixin, DetailView):
    """Download a .docx with a printable scorecard for every entrant (editor-only)."""

    model = Division
    context_object_name = "division"

    def render_to_response(self, context, **kwargs):
        division = self.object
        tournament = division.tournament
        rounds = make_rounds(division.configured_round_numbers())
        qr_url = self.request.build_absolute_uri(
            reverse("published_pairings", kwargs=division.slug_kwargs())
        )
        opponents, starts = self._prefills_by_entrant(division)
        # The director opts in to prefilling submitted results per download.
        include_results = bool(self.request.GET.get("include_results"))
        results = self._results_by_entrant(division) if include_results else {}
        specs = [
            ScorecardSpec(
                tournament_name=tournament.name,
                tournament_date=tournament.start_date.strftime("%B %-d, %Y"),
                player_name=entrant.name,
                rounds=rounds,
                opponents=opponents.get(entrant.pk, {}),
                starts=starts.get(entrant.pk, {}),
                results=results.get(entrant.pk, {}),
                qr_url=qr_url,
            )
            # Withdrawn players don't play further rounds, so they get no card.
            for entrant in division.entrants.filter(dropped=False)
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
            # A bye: record "Bye" as the real player's opponent, with no start,
            # and nothing for the bye entrant itself (it has no scorecard).
            if p.first.player.is_bye:
                opponents[p.second_id][p.round] = p.first.name
                continue
            if p.second.player.is_bye:
                opponents[p.first_id][p.round] = p.second.name
                continue
            opponents[p.first_id][p.round] = p.second.name
            opponents[p.second_id][p.round] = p.first.name
            starts[p.first_id][p.round] = "1st"
            starts[p.second_id][p.round] = "2nd"
        return opponents, starts

    @staticmethod
    def _results_by_entrant(division):
        """Map each entrant id to its {round: ScorecardResult} for submitted
        results, from that entrant's point of view. Byes are skipped (they are
        materialized as results but aren't a played game the player records);
        the running record (wins/losses, a tie counting half to each) and the
        cumulative spread are accumulated in round order."""
        slips = division.result_slips.select_related(
            "winner__player", "loser__player"
        ).order_by("round")
        results = defaultdict(dict)
        wins = defaultdict(float)
        losses = defaultdict(float)
        spread = defaultdict(int)
        for slip in slips:
            if slip.winner.player.is_bye or slip.loser.player.is_bye:
                continue
            tie = slip.winner_score == slip.loser_score
            for entrant_id, pscore, oscore in (
                (slip.winner_id, slip.winner_score, slip.loser_score),
                (slip.loser_id, slip.loser_score, slip.winner_score),
            ):
                if tie:
                    wins[entrant_id] += 0.5
                    losses[entrant_id] += 0.5
                elif pscore > oscore:
                    wins[entrant_id] += 1
                else:
                    losses[entrant_id] += 1
                spread[entrant_id] += pscore - oscore
                results[entrant_id][slip.round] = ScorecardResult(
                    player_score=pscore,
                    opponent_score=oscore,
                    cumulative_wins=wins[entrant_id],
                    cumulative_losses=losses[entrant_id],
                    cumulative_spread=spread[entrant_id],
                )
        return results


class DivisionResultsExportView(LoginRequiredMixin, CanEditDivisionMixin, DetailView):
    """Download a division's results as CSV in the coco-ratings format (editor-only)."""

    model = Division
    context_object_name = "division"

    def render_to_response(self, context, **kwargs):
        division = self.object
        tournament = division.tournament
        slips = division.result_slips.select_related(
            "winner__player", "loser__player"
        ).order_by("round", "created_at")
        rows = [
            ResultRow(
                round=slip.round,
                winner=slip.winner.name,
                winner_score=slip.winner_score,
                opponent=slip.loser.name,
                opponent_score=slip.loser_score,
                submitted_on=slip.created_at,
            )
            for slip in slips
            # A bye is materialized as a result but isn't a played game; leave it
            # out so it doesn't create a phantom "Bye" player in the ratings.
            if not (slip.winner.player.is_bye or slip.loser.player.is_bye)
        ]

        response = HttpResponse(render_results_csv(rows), content_type="text/csv")
        filename = slugify(f"{tournament.name}-{division.name}-results") + ".csv"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response


class DivisionSettingsEditView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Placeholder for future per-division configuration. Round pairings, which
    used to live here, now have their own tab."""

    template_name = "tournaments/division_settings_edit.html"

    def get(self, request, *args, **kwargs):
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

    def get(self, request, *args, **kwargs):
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
                "edit_presence",
                kwargs={**division.slug_kwargs(), "scope": "round_pairings"},
            ),
            "preview_url": reverse(
                "division_round_pairings_preview", kwargs=division.slug_kwargs()
            ),
            "active_tab": "round_pairings",
            "can_edit": True,
        })

    def post(self, request, *args, **kwargs):
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
            save_settings(
                division.tournament, request.user,
                {"division": division.name, "blocks": blocks},
            )
        return guard.response


class DivisionRoundPairingsPreviewView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Expand blocks into the per-round list without saving — drives the live
    preview table."""

    def post(self, request, *args, **kwargs):
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
            "edit_presence",
            kwargs={**division.slug_kwargs(), "scope": self.grid.scope},
        )

    def get_context_data(self, division):
        context = super().get_context_data(division)
        context.update(
            {"division": division, "active_tab": self.active_tab, "can_edit": True}
        )
        return context

    def save_context(self):
        # Run the grid write inside an event-log command context.
        from tournaments.events import command_context

        return command_context()

    def on_saved(self, division, rows):
        # Record a grid-save event (in the same transaction) with a pk-free,
        # replay-safe payload. Grids without an event_type opt out.
        from tournaments.events import division_digest, record_event

        if not self.grid.event_type:
            return
        actor = self.request.user if self.request.user.is_authenticated else None
        record_event(
            division.tournament,
            self.grid.event_type,
            {"division": division.name, "rows": self.grid.to_portable(rows, division)},
            actor=actor,
            division=division,
            digest=division_digest(division),
        )


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

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        uploaded = request.FILES.get("csv_file")
        if not uploaded:
            return JsonResponse({"errors": ["No file uploaded."]}, status=400)

        try:
            text = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError:
            return JsonResponse({"errors": ["File must be UTF-8 encoded text."]}, status=400)

        result, errors = bulk_import_entrants(
            division.tournament, request.user,
            {"division": division.name, "csv": text},
        )
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

    def get(self, request, *args, **kwargs):
        division = self.get_division()
        context = {"division": division, "active_tab": "fixtures", "can_edit": True}
        slug_kwargs = division.slug_kwargs()
        for grid, route, ctx_name in self.GRIDS:
            context[ctx_name] = build_grid_context(
                grid,
                division,
                key=edit_key(division, grid.scope),
                presence_url=reverse(
                    "edit_presence", kwargs={**slug_kwargs, "scope": grid.scope}
                ),
                save_url=reverse(route, kwargs=slug_kwargs),
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
    "winner_score": "", "loser_score": "",
    "verified_by_opponent": False,
}


def _result_initial(result):
    return {
        "round": result.round,
        "pairing": result.pairing_id,
        "winner": result.winner_id,
        "winner_score": result.winner_score,
        "loser_score": result.loser_score,
    }


def _result_signals(result):
    return {
        "round": str(result.round),
        "pairing": str(result.pairing_id),
        "winner": str(result.winner_id),
        "winner_score": result.winner_score,
        "loser_score": result.loser_score,
        # Always require re-confirmation, even when editing an existing result.
        "verified_by_opponent": False,
    }


def _result_summary(result):
    return (
        f"R{result.round}: {result.winner_name} {result.winner_score}"
        f"–{result.loser_score} {result.loser_name}"
    )


class ResultSlipCreateView(DivisionURLMixin, View):
    template_name = "tournaments/resultslip_form.html"

    def get_division(self):
        _ensure_visible_division(self.division, self.request.user)
        return self.division

    # Session key holding the pks of result slips this browser created, so an
    # anonymous submitter can edit their own submissions without being able to
    # edit anyone else's by guessing pks.
    SESSION_KEY = "created_result_pks"

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

    def _remember_created_result(self, request, rs):
        # Re-assign (rather than mutate in place) so the session is reliably
        # marked dirty and persisted.
        pks = request.session.get(self.SESSION_KEY, [])
        if rs.pk not in pks:
            request.session[self.SESSION_KEY] = pks + [rs.pk]

    def _can_edit_result(self, request, division, result):
        """Whether the caller may edit an existing slip.

        Tournament editors may always edit. An anonymous submitter may edit only
        a slip their own session created, and only while the round is still open
        — once the round is FINISHED, corrections go through a director.
        """
        if division.tournament.can_edit(request.user):
            return True
        if result.pk not in request.session.get(self.SESSION_KEY, []):
            return False
        rp = division.round_pairings_set.filter(round=result.round).first()
        return rp is None or rp.status != RoundPairings.FINISHED

    def _guard_edit(self, request, division, result):
        """Raise PermissionDenied if ``result`` is being edited without rights."""
        if result is not None and not self._can_edit_result(request, division, result):
            raise PermissionDenied

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
        slug_kwargs = division.slug_kwargs()
        if editing and result is not None:
            form_action = reverse(
                "resultslip_edit", kwargs={**slug_kwargs, "result_pk": result.pk}
            )
        else:
            form_action = reverse("resultslip_create", kwargs=slug_kwargs)
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
                "edit_url": reverse(
                    "resultslip_edit", kwargs={**slug_kwargs, "result_pk": saved.pk}
                ),
            }
        return context

    def _render(self, request, context, signals=None):
        if is_datastar(request):
            return fragment_response(
                "tournaments/_resultslip_form.html", context,
                request=request, signals=signals,
            )
        return render(request, self.template_name, context)

    def _render_played(self, request, division, result):
        """Show an already-submitted game's result instead of a fresh form."""
        slug_kwargs = division.slug_kwargs()
        context = {
            "division": division,
            "result": result,
            "active_tab": "add_result",
            "can_edit": division.tournament.can_edit(request.user),
            "pairings_url": reverse("division_pairings", kwargs=slug_kwargs),
            "edit_url": reverse(
                "resultslip_edit", kwargs={**slug_kwargs, "result_pk": result.pk}
            ),
        }
        return render(request, "tournaments/resultslip_played.html", context)

    def _prefill_pairing(self, division, pairing_pk):
        """The pairing named by a ``?pairing=`` param, if it belongs to this
        division. Lets the published pairings page deep-link a match into the
        form with its round and pairing pre-selected."""
        if not pairing_pk:
            return None
        return (
            Pairing.objects
            .select_related("first__player", "second__player", "round_pairings")
            .filter(division=division, pk=pairing_pk)
            .first()
        )

    def get(self, request, *args, **kwargs):
        division = self.get_division()
        result = self.get_result(division)
        self._guard_edit(request, division, result)
        if result is not None:
            pbr = _pairings_by_round(division, include_pairing=result.pairing)
            form = ResultSlipForm(
                division=division, pairings_by_round=pbr,
                instance=result, initial=_result_initial(result),
            )
            context = self._form_context(division, form, editing=True, result=result)
            return self._render(request, context, signals=_result_signals(result))
        prefill = self._prefill_pairing(division, request.GET.get("pairing"))
        # A stale pairings page can point "Submit results" at a game that has
        # since been played; show its result instead of a blank submission form.
        existing = prefill.result if prefill is not None and hasattr(prefill, "result") else None
        if existing is not None:
            return self._render_played(request, division, existing)
        pbr = _pairings_by_round(division, include_pairing=prefill)
        signals = _BLANK_RESULT_SIGNALS
        # Pre-select the match's round and pairing. ``initial`` drives the
        # rendered <select>s for a full-page load (the published-pairings link);
        # ``signals`` patches the same values for a datastar fragment load.
        initial = None
        if prefill is not None:
            initial = {"round": prefill.round, "pairing": prefill.pk}
            signals = {**signals, "round": str(prefill.round), "pairing": str(prefill.pk)}
        form = ResultSlipForm(division=division, pairings_by_round=pbr, initial=initial)
        context = self._form_context(division, form)
        return self._render(request, context, signals=signals)

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        result = self.get_result(division)
        self._guard_edit(request, division, result)
        creating = result is None
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
            pairing = form.cleaned_data["pairing"]
            winner = form.cleaned_data["winner"]
            payload = {
                "division": division.name,
                "round": pairing.round,
                "first_name": pairing.first.player.name,
                "second_name": pairing.second.player.name,
                "winner_name": winner.player.name,
                "winner_score": form.cleaned_data["winner_score"],
                "loser_score": form.cleaned_data["loser_score"],
            }
            actor = request.user if request.user.is_authenticated else None
            command = add_result if creating else edit_result
            rs = command(division.tournament, actor, payload)
            if creating:
                # Let this browser edit its own submission later without opening
                # up pk-guessing edits of everyone else's slips.
                self._remember_created_result(request, rs)
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

    def post(self, request, *args, **kwargs):
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

        simulate_match_cmd(
            division.tournament, request.user,
            {"division": division.name, "round": round_num,
             "first_name": first_name, "second_name": second_name},
        )
        return _simulation_response(request, division)


class SimulateRoundView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Simulate all remaining matches in a round for a test division."""

    def post(self, request, *args, **kwargs):
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

        simulate_round_cmd(
            division.tournament, request.user,
            {"division": division.name, "round": round_num},
        )
        return _simulation_response(request, division)
