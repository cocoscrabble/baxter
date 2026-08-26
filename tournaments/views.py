import json
from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse, reverse_lazy
from django.utils.text import slugify
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from .datastar_utils import fragment_response, is_datastar
from .display import division_labels, label_entrants, label_standings
from .player_source import get_player_source
from datastar_py.django import read_signals
from .forms import (
    CopConfigForm,
    FakeTournamentForm,
    GuestForm,
    RegistrationForm,
    ResultSlipForm,
    TournamentForm,
    WhatIfImportForm,
)
from .fake_tournament import create_fake_tournament, default_fake_tournament_name
from .fixed_pairings import (
    add_fixed_pairing,
    remove_fixed_pairing,
    remove_fixed_pairings,
)
from .match_simulation import simulate_match, simulate_round
from .commands import (
    add_entrant,
    add_fixed_pairing_cmd,
    add_result,
    bulk_import_entrants,
    create_division,
    create_player,
    update_entrant,
    create_playoff,
    create_tournament,
    edit_result,
    delete_division,
    delete_playoff,
    delete_tournament,
    import_division,
    publish_all_rounds,
    publish_round,
    remove_fixed_pairing_cmd,
    remove_fixed_pairings_cmd,
    rename_division,
    restore_division,
    save_cop_config,
    save_settings,
    simulate_match_cmd,
    simulate_round_cmd,
    unpublish_round,
    update_playoff,
    update_tournament,
)
from .grids import BoardTableMapGrid, EntrantsGrid, FixedPairingsGrid, FixedTablesGrid, ResultsGrid
from .models import (
    EDIT_SCOPES,
    Division,
    Entrant,
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
    RP,
    blocks_to_round_pairings,
    default_block_rounds,
    round_pairings_to_blocks,
)
from .pairing.methods import (
    PairingMethod,
    pairing_method_schedule,
)
from .player_sync import import_players
from .roster_import import RosterParseError, import_roster
from .wespa_ratings import parse_wespa_csv, refresh_wespa_ratings
from users.models import User
from .generate_pairings import publish_rounds, regenerate_pairings, unpublish_rounds
from .pairing.base import PairingData, PairingError, standings_after_round
from .pairing.round_pairing import STRATEGY_TYPES
from .pairings_view import PairingsPresenter, PublishedPairingsPresenter
from .playoff import (
    QUALIFIER_COUNTS,
    SERIES_LABELS,
    Timing,
    build_bracket,
    default_stage_games,
    final_placements,
    placement_keys,
    playoff_for,
    qualification_seeds,
    schedule_conflicts,
    selectable_qualification_rounds,
    series_keys,
    validate_config,
)
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


def _default_whatif_name(source_name, filename=None):
    from pathlib import Path

    stem = Path(filename).stem if filename else None
    label = source_name or stem or "import"
    stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
    return f"What-if: {label} {stamp}"


class WhatIfImportView(LoginRequiredMixin, View):
    """Import a historical division (JSON bundle or coco-ratings CSV) into a
    sandbox tournament the user owns, for what-if exploration. GET renders the
    form; POST parses, builds the sandbox via commands, and shows a summary."""

    template_name = "tournaments/whatif_import.html"

    def get(self, request):
        return render(request, self.template_name, {"form": WhatIfImportForm()})

    def post(self, request):
        from .whatif_import import ImportParseError, parse_import

        form = WhatIfImportForm(request.POST, request.FILES)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})

        upload = form.cleaned_data.get("upload")
        filename = upload.name if upload is not None else None
        if upload is not None:
            try:
                text = upload.read().decode("utf-8")
            except UnicodeDecodeError:
                form.add_error("upload", "The file isn't valid UTF-8 text.")
                return render(request, self.template_name, {"form": form})
        else:
            text = form.cleaned_data["pasted"]

        try:
            source_name, divisions = parse_import(text)
        except ImportParseError as e:
            form.add_error(None, str(e))
            return render(request, self.template_name, {"form": form})

        name = form.cleaned_data.get("name") or _default_whatif_name(source_name, filename)
        # All-or-nothing: a failure importing any division rolls back the whole
        # sandbox (each command is atomic; the outer atomic ties them together).
        with transaction.atomic():
            tournament = create_tournament(None, request.user, {
                "name": name,
                "location": "What-if sandbox",
                "start_date": timezone.now().date().isoformat(),
                "is_fake": True,
                "default_division": {"name": divisions[0]["name"]},
            })
            summaries = [
                import_division(tournament, request.user, d) for d in divisions
            ]

        first_division = tournament.divisions.get(name=divisions[0]["name"])
        return render(request, "tournaments/whatif_import_summary.html", {
            "tournament": tournament,
            "division": first_division,
            "summaries": summaries,
        })


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


class PubliclyVisibleDivisionMixin(DivisionURLMixin):
    """Resolve the division **as a signed-out visitor would see it**.

    For a page that is embedded in someone else's site: an embedded view must
    contain strictly what an anonymous request would get, and nothing more.

    The distinction matters because an iframe is loaded by the *visitor's*
    browser, carrying the visitor's cookies. A director browsing the CoCo site
    while signed into Baxter would otherwise be served a different page from
    everyone else — a test division, say, which ``VisibleDivisionMixin`` shows
    to editors. Nobody chose to visit an embed; it must not vary by who is
    looking.

    Views using this must also refuse to render editor-only fields regardless of
    ``request.user`` — see ``DivisionEntrantsEmbedView``.
    """

    def get_object(self, queryset=None):
        _ensure_visible_division(self.division, AnonymousUser())
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


class TournamentActivityView(TournamentURLMixin, LoginRequiredMixin, CanEditTournamentMixin, DetailView):
    """The tournament's event log, newest first (owners/editors only)."""

    model = Tournament
    template_name = "tournaments/tournament_activity.html"
    context_object_name = "tournament"

    def get_context_data(self, **kwargs):
        from tournaments.events import describe_event

        context = super().get_context_data(**kwargs)
        events = self.object.events.order_by("-seq").select_related(
            "actor", "division"
        )
        context["events"] = [(e, describe_event(e)) for e in events]
        return context


class TournamentEventLogExportView(TournamentURLMixin, LoginRequiredMixin, CanEditTournamentMixin, View):
    """Download the tournament's event log as JSONL (owners/editors only)."""

    def get_object(self, queryset=None):
        return self.tournament

    def get(self, request, *args, **kwargs):
        from tournaments.events import export_jsonl

        content = export_jsonl(self.tournament)
        response = HttpResponse(content, content_type="application/x-ndjson")
        response["Content-Disposition"] = (
            f'attachment; filename="{self.tournament.slug}-eventlog.jsonl"'
        )
        return response


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
            slips = list(
                division.result_slips
                .filter(round=max_round)
                .select_related("winner__player", "loser__player")
                .order_by("-created_at")
            )
            labels = division_labels(division)
            label_entrants(
                labels,
                (s.winner for s in slips),
                (s.loser for s in slips),
            )
            context["latest_results"] = slips
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
        slips = list(
            self.object.result_slips
            .select_related("winner__player", "loser__player")
            .order_by("-created_at")
        )
        labels = division_labels(self.object)
        label_entrants(
            labels, (s.winner for s in slips), (s.loser for s in slips)
        )
        context["result_slips"] = slips
        return context


def entrants_for_display(division):
    """The division's entrants in seed order, with their display names and the
    flags the legend keys off.

    Seed order is by the *pinned* rating — the one the division was actually
    seeded from — with ties broken by entrant number.

    The legend only lists markers that are actually on the page: a table with no
    tentative entrants should not explain what an asterisk would have meant.
    """
    entrants = list(
        division.entrants.select_related("player").order_by("-rating", "number")
    )
    label_entrants(division_labels(division), entrants)
    flags = {
        "has_tentative": any(e.tentative for e in entrants),
        "has_playing_up": any(e.playing_up for e in entrants),
        "has_wespa": any(e.rating_source == Entrant.WESPA for e in entrants),
    }
    return entrants, {**flags, "has_markers": any(flags.values())}


class DivisionEntrantsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_entrants.html"
    context_object_name = "division"
    active_tab = "entrants"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entrants, flags = entrants_for_display(self.object)
        context["entrants"] = entrants
        context.update(flags)
        return context


@method_decorator(xframe_options_exempt, name="dispatch")
class DivisionEntrantsEmbedView(PubliclyVisibleDivisionMixin, DetailView):
    """The entrant list as a chrome-free fragment, for the CoCo site to iframe.

    **This view contains strictly what a signed-out visitor would get.** An
    iframe is loaded by the visitor's browser with the visitor's cookies, so a
    director browsing the CoCo site while signed into Baxter must be served the
    same bytes as everyone else. Two things enforce that, and both are needed:

    - ``PubliclyVisibleDivisionMixin`` resolves the division as anonymous, so a
      test division is a 404 even for the organizer who owns it.
    - ``can_edit`` is forced False, so the payment column is not rendered for a
      signed-in editor either.

    ``xframe_options_exempt`` is not optional either: Django's
    XFrameOptionsMiddleware defaults to SAMEORIGIN, so without it the embedding
    page renders a blank box with a console error and no other clue what went
    wrong.
    """

    model = Division
    template_name = "tournaments/division_entrants_embed.html"
    context_object_name = "division"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        entrants, flags = entrants_for_display(self.object)
        context["entrants"] = entrants
        context.update(flags)
        context["can_edit"] = False
        return context


def division_standings(division, current_round):
    """Return the standings list after ``current_round``, annotated with each
    entrant's seed number and dropped flag (as ``_standings_table.html`` expects).

    Display standings keep withdrawn players visible (marked ``dropped`` below);
    their games are always counted in everyone's record.
    """
    pd = PairingData.for_division(division)
    standings = standings_after_round(pd, current_round, include_dropped=True)
    entrants = list(division.entrants.select_related("player"))
    seed_by_key = {e.key: e.number for e in entrants}
    dropped_keys = {e.key for e in entrants if e.dropped}
    for p in standings:
        p.seed = seed_by_key.get(p.key)
        p.dropped = p.key in dropped_keys
    label_standings(division, standings)
    return standings


class DivisionStandingsView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    model = Division
    template_name = "tournaments/division_standings.html"
    context_object_name = "division"
    active_tab = "standings"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        division = self.object
        max_round = division.max_round()
        current_round = self.kwargs.get("round", max_round)
        context["standings"] = division_standings(division, current_round)
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


class DivisionStandingsTableView(VisibleDivisionMixin, DetailView):
    """Return the current standings as a bare ``<table>`` HTML fragment, intended
    to be embedded in other pages (no navbar, tabs, or page chrome). Defaults to
    the latest round; an optional ``round`` kwarg pins an earlier round."""

    model = Division

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        division = self.object
        current_round = self.kwargs.get("round", division.max_round())
        html = render_to_string(
            "tournaments/_standings_table.html",
            {
                "standings": division_standings(division, current_round),
                # Firsts is an organiser-only column; keep the public embed lean.
                "can_edit": False,
            },
            request=request,
        )
        return HttpResponse(html)




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
                    "player1": e1.key, "player2": e2.key,
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
                    "player1": fp.entrant1.key,
                    "player2": fp.entrant2.key,
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
                k1, k2 = sorted([fp.entrant1.key, fp.entrant2.key])
                kept.append([fp.round_number, k1, k2])
        error = remove_fixed_pairings_cmd(
            division.tournament, request.user,
            {"division": division.name, "kept": kept},
        )
        if error:
            messages.error(request, error)
        return redirect("division_pair_rounds", **division.slug_kwargs())


def _entrants_for_editing(division):
    entrants = list(
        division.entrants.select_related("player").order_by("player__name")
    )
    # The fixed-pairing pickers list these by name, so same-named entrants must
    # be told apart before the director picks one of them.
    label_entrants(division_labels(division), entrants)
    return entrants


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


def _int_or(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class DivisionExploreView(LoginRequiredMixin, DivisionNavMixin, CanEditDivisionMixin, DetailView):
    """"Explore" tab: hypothetically re-pair one round with a chosen strategy off
    a chosen based-on round. Pure read — nothing is persisted. Editor-only, like
    Pair rounds. Query params (round, strategy, based_on, seed) make results
    shareable; a datastar request swaps just the result panel."""

    model = Division
    template_name = "tournaments/division_explore.html"
    context_object_name = "division"
    active_tab = "explore"

    def get_context_data(self, **kwargs):
        from .whatif import (
            actual_rows,
            configured_pairing,
            decorate,
            explore_pairing,
            mark_common,
        )

        context = super().get_context_data(**kwargs)
        division = self.object
        max_round = division.max_round()
        strategies = [str(s) for s in STRATEGY_TYPES]
        get = self.request.GET
        # Only pair when the user actually asks (the Pair/Reshuffle button, or a
        # shared URL, carries `round`). A bare tab visit shows a placeholder, so
        # the page loads without an engine call.
        explored = "round" in get

        # Round to pair: 1 .. max_round + 1 (the +1 explores the next round).
        target = _int_or(get.get("round"), max(max_round, 1))
        target = max(1, min(target, max_round + 1))
        # Based-on round: 0 (seedings) .. target - 1.
        based_on = _int_or(get.get("based_on"), target - 1)
        based_on = max(0, min(based_on, target - 1))
        strategy = get.get("strategy") or "Swiss"
        if strategy not in strategies:
            strategy = "Swiss"
        seed = _int_or(get.get("seed"), _division_seed(division))

        error, rows, actual = None, [], None
        actual_strategy, actual_based_on = "", None
        if explored:
            from .pairing.base import PairingData

            # Build the PairingData once and share it across the helpers below
            # (each would otherwise rebuild it — a costly per-call query set).
            pd = PairingData.for_division(division)
            try:
                pairings = explore_pairing(division, target, strategy, based_on, seed, pd=pd)
                rows = decorate(division, target, based_on, pairings, pd=pd)
                # Compare against reality only when the target round was played.
                actual = actual_rows(division, target, pd=pd) if target <= max_round else None
                rows, actual = mark_common(rows, actual)
                configured = configured_pairing(division, target, pd=pd) if actual else None
                if configured is not None:
                    actual_strategy = configured.pairing
                    actual_based_on = configured.start_round
            except PairingError as e:
                error = str(e)

        context.update({
            "target_round": target,
            "based_on": based_on,
            "strategy": strategy,
            "seed": seed,
            "explored": explored,
            "explore_rows": rows,
            "actual_rows": actual,
            "actual_strategy": actual_strategy,
            "actual_based_on": actual_based_on,
            "explore_error": error,
            "strategies": strategies,
            "round_choices": range(1, max_round + 2),
            "based_on_choices": range(0, max_round + 1),
            "is_random_strategy": strategy in ("Random", "RandomNoRepeats", "SwissPlusRandom"),
        })
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        if is_datastar(request):
            return fragment_response(
                "tournaments/_explore_content.html", context, request=request
            )
        return self.render_to_response(context)


def _division_seed(division):
    try:
        return division.settings.pairing_seed
    except DivisionSettings.DoesNotExist:
        return 0


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
        # A scorecard names its player and their opponents, so a shared name
        # has to be disambiguated or two people get each other's card.
        card_entrants = list(
            division.entrants.filter(dropped=False).select_related("player")
        )
        label_entrants(division_labels(division), card_entrants)
        specs = [
            ScorecardSpec(
                tournament_name=tournament.name,
                tournament_date=tournament.start_date.strftime("%B %-d, %Y"),
                player_name=entrant.display_name,
                rounds=rounds,
                opponents=opponents.get(entrant.pk, {}),
                starts=starts.get(entrant.pk, {}),
                results=results.get(entrant.pk, {}),
                qr_url=qr_url,
            )
            # Withdrawn players don't play further rounds, so they get no card.
            for entrant in card_entrants
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
        pairings = list(
            division.pairings.select_related("first__player", "second__player")
        )
        label_entrants(
            division_labels(division),
            (p.first for p in pairings),
            (p.second for p in pairings),
        )
        opponents = defaultdict(dict)
        starts = defaultdict(dict)
        for p in pairings:
            # A bye: record "Bye" as the real player's opponent, with no start,
            # and nothing for the bye entrant itself (it has no scorecard).
            if p.first.player.is_bye:
                opponents[p.second_id][p.round] = p.first.display_name
                continue
            if p.second.player.is_bye:
                opponents[p.first_id][p.round] = p.second.display_name
                continue
            opponents[p.first_id][p.round] = p.second.display_name
            opponents[p.second_id][p.round] = p.first.display_name
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
                # The plain name, not the disambiguated label: this column is a
                # join key for coco-ratings, and the number columns beside it are
                # what resolve a shared name.
                winner=slip.winner.name,
                winner_number=slip.winner.key,
                winner_score=slip.winner_score,
                opponent=slip.loser.name,
                opponent_number=slip.loser.key,
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
    """Per-division configuration. Currently the COP pairing strategy's prize +
    tuning settings (round pairings have their own tab)."""

    template_name = "tournaments/division_settings_edit.html"

    def get(self, request, *args, **kwargs):
        division = self.get_division()
        cop_config = self._cop_config(division)
        form = CopConfigForm(initial=cop_config or None)
        return render(request, self.template_name, self._context(division, form))

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        form = CopConfigForm(request.POST)
        if form.is_valid():
            save_cop_config(
                division.tournament, request.user,
                {"division": division.name, "cop_config": form.to_config()},
            )
            messages.success(request, "COP settings saved.")
            return redirect("division_settings", **division.slug_kwargs())
        return render(request, self.template_name, self._context(division, form))

    def _cop_config(self, division) -> dict:
        try:
            return division.settings.cop_config or {}
        except DivisionSettings.DoesNotExist:
            return {}

    def _uses_cop(self, division) -> bool:
        try:
            rps = division.settings.round_pairings or []
        except DivisionSettings.DoesNotExist:
            return False
        return any(rp.get("pairing") == str(RP.COP) for rp in rps)

    def _context(self, division, form) -> dict:
        return {
            "division": division,
            "active_tab": "settings",
            "can_edit": True,
            "form": form,
            "uses_cop": self._uses_cop(division),
        }


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
        method_total_rounds = len(preview) or 24
        key = edit_key(division, "round_pairings")
        return render(request, self.template_name, {
            "division": division,
            "blocks_json": json.dumps(blocks),
            "preview_json": json.dumps(preview),
            "default_rounds_json": json.dumps(default_block_rounds(division.entrants.count())),
            "strategy_types_json": json.dumps([str(s) for s in STRATEGY_TYPES]),
            "pairing_methods": [(str(m), m.label) for m in PairingMethod],
            "method_total_rounds": method_total_rounds,
            "edit_version": EditVersion.version_for(key),
            "presence_url": reverse(
                "edit_presence",
                kwargs={**division.slug_kwargs(), "scope": "round_pairings"},
            ),
            "preview_url": reverse(
                "division_round_pairings_preview", kwargs=division.slug_kwargs()
            ),
            "method_preview_url": reverse(
                "division_pairing_method_preview", kwargs=division.slug_kwargs()
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


class DivisionPairingMethodPreviewView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Compile a first-class pairing method to editable schedule blocks."""

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        try:
            data = json.loads(request.body)
            method = PairingMethod(data.get("method"))
            total_rounds = int(data.get("total_rounds"))
            schedule = pairing_method_schedule(
                method,
                entrants=division.entrants.filter(dropped=False).count(),
                total_rounds=total_rounds,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            return JsonResponse({"errors": [str(error)]}, status=400)

        rows = [rp.to_dict() for rp in blocks_to_round_pairings(schedule.blocks)]
        return JsonResponse({
            "ok": True,
            "method": str(schedule.method),
            "blocks": schedule.blocks,
            "rows": rows,
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


class DivisionRegisterView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """Register entrants one at a time — the primary *add* flow.

    The edit grid stays the bulk surface (seat numbers, dropped, quick flag
    edits); this page is where a director adds someone, creates a guest, or
    confirms/fixes one entrant without touching a spreadsheet.

    Three actions, all POSTing here:

    - ``add`` — an existing player, chosen by number.
    - ``guest`` — a name and a rating, which mints a provisional player and
      enters them in one step (decision 4).
    - ``update`` — the same registration fields over an entrant that exists.

    Player lookup goes through ``player_source`` so the same page works against
    the central roster later without any change here (decision 11).
    """

    template_name = "tournaments/division_register.html"
    GUEST_PREFIX = "guest"

    def get(self, request, *args, **kwargs):
        division = self.get_division()
        overrides = {}
        if request.GET.get("entrant"):
            entrant = division.entrants.filter(
                pk=request.GET["entrant"]
            ).select_related("player").first()
            if entrant is None:
                messages.error(request, "That entrant is not in this division.")
                return self._redirect(division)
            overrides["editing"] = entrant
            overrides["registration_form"] = RegistrationForm(
                initial={
                    "number": entrant.number,
                    "rating": entrant.rating,
                    "tentative": entrant.tentative,
                    "paid": entrant.paid,
                    "playing_up": entrant.playing_up,
                    "payment_note": entrant.payment_note,
                }
            )
        elif "q" in request.GET:
            query, results = self._search(request, division)
            overrides["search_query"] = query
            overrides["search_results"] = results
        return render(
            request, self.template_name, self._context(division, **overrides)
        )

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        action = request.POST.get("action")
        handler = {
            "add": self._add,
            "guest": self._guest,
            "update": self._update,
        }.get(action)
        if handler is None:
            messages.error(request, "Unknown action.")
            return redirect("division_register", **division.slug_kwargs())
        return handler(request, division)

    # -- context -----------------------------------------------------------

    def _next_number(self, division):
        return (
            division.entrants.aggregate(m=models.Max("number"))["m"] or 0
        ) + 1

    def _context(self, division, **overrides):
        entrants = list(
            division.entrants.select_related("player").order_by("number")
        )
        label_entrants(division_labels(division), entrants)
        context = {
            "division": division,
            "tournament": division.tournament,
            "entrants": entrants,
            "can_edit": True,
            "active_tab": "entrants",
            "registration_form": RegistrationForm(
                initial={"number": self._next_number(division)}
            ),
            # The same fieldset appears twice on this page, so the guest copy is
            # prefixed. Without it both render identical element ids and the
            # guest form's labels silently point at the add form's inputs.
            "guest_registration_form": RegistrationForm(
                prefix=self.GUEST_PREFIX,
                initial={"number": self._next_number(division)},
            ),
            "guest_form": GuestForm(prefix=self.GUEST_PREFIX),
            "search_query": "",
            "search_results": [],
            "editing": None,
        }
        context.update(overrides)
        return context

    def _search(self, request, division):
        query = request.GET.get("q", "")
        found = get_player_source().search(query)
        entered = set(division.entrants.values_list("player__player_number", flat=True))
        return query, [r for r in found if r.player_number not in entered]

    # -- actions -----------------------------------------------------------

    def _redirect(self, division):
        return redirect("division_register", **division.slug_kwargs())

    def _add(self, request, division):
        form = RegistrationForm(request.POST)
        record = get_player_source().fetch(request.POST.get("player", ""))
        if record is None:
            messages.error(request, "Pick a player first.")
            return render(request, self.template_name, self._context(division))
        if not form.is_valid():
            return render(
                request, self.template_name,
                self._context(division, registration_form=form),
            )
        payload = {
            "division": division.name,
            "player": record.player_number,
            **form.registration(),
        }
        try:
            add_entrant(division.tournament, request.user, payload)
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(
                request, self.template_name,
                self._context(division, registration_form=form),
            )
        messages.success(request, f"Entered {record.name}.")
        return self._redirect(division)

    def _guest(self, request, division):
        form = RegistrationForm(request.POST, prefix=self.GUEST_PREFIX)
        guest = GuestForm(request.POST, prefix=self.GUEST_PREFIX)
        if not (form.is_valid() and guest.is_valid()):
            return render(
                request, self.template_name,
                self._context(
                    division, guest_registration_form=form, guest_form=guest
                ),
            )
        source = get_player_source()
        number = source.mint_number(guest.cleaned_data["name"])
        create_player(
            division.tournament, request.user,
            {
                "player_number": number,
                "name": guest.cleaned_data["name"],
                # No CoCo rating by definition — that is what makes them a guest.
                "rating": 0,
                "wespa_rating": guest.cleaned_data["wespa_rating"],
            },
        )
        try:
            add_entrant(
                division.tournament, request.user,
                {
                    "division": division.name,
                    "player": number,
                    **form.registration(),
                },
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(
                request, self.template_name,
                self._context(
                    division, guest_registration_form=form, guest_form=guest
                ),
            )
        messages.success(request, f"Entered {guest.cleaned_data['name']}.")
        return self._redirect(division)

    def _update(self, request, division):
        entrant = division.entrants.filter(
            pk=request.POST.get("entrant")
        ).select_related("player").first()
        if entrant is None:
            messages.error(request, "That entrant is no longer in this division.")
            return self._redirect(division)
        form = RegistrationForm(request.POST)
        if not form.is_valid():
            return render(
                request, self.template_name,
                self._context(division, registration_form=form, editing=entrant),
            )
        registration = form.registration()
        # The form prefills the current rating, so a director who opens this
        # page and saves without touching it must not thereby convert a `coco`
        # snapshot into a `manual` one. Only an actual change is an override.
        if registration.get("rating") == entrant.rating:
            registration.pop("rating")
        try:
            update_entrant(
                division.tournament, request.user,
                {
                    "division": division.name,
                    "player": entrant.key,
                    **registration,
                },
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return self._redirect(division)
        messages.success(request, f"Updated {entrant.player.name}.")
        return self._redirect(division)


class DivisionRegisterSearchView(LoginRequiredMixin, CanEditDivisionMixin, View):
    """The registration page's player search, as an HTML fragment.

    Its own endpoint so the search can be re-run without re-posting the
    registration form, and so swapping in a registry-backed source later touches
    nothing else.
    """

    def get(self, request, *args, **kwargs):
        division = self.get_division()
        query = request.GET.get("q", "")
        entered = set(
            division.entrants.values_list("player__player_number", flat=True)
        )
        results = [
            r for r in get_player_source().search(query)
            if r.player_number not in entered
        ]
        return render(
            request,
            "tournaments/_register_results.html",
            {"division": division, "search_query": query, "search_results": results},
        )


class CreatePlayerView(LoginRequiredMixin, View):
    """AJAX endpoint to create a new Player and return its data.

    Two players may share a name — the number is the identity — but a repeated
    name is usually a typo, so an unconfirmed request that matches existing
    players returns them (409) instead of creating a twin. The client shows who
    they are and resends with ``confirm`` if the director really means it.
    """

    def post(self, request):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON."}, status=400)

        name = data.get("name")
        if not data.get("confirm"):
            existing = Player.same_named(name)
            if existing.exists():
                return JsonResponse(
                    {
                        "duplicate_name": True,
                        "name": (name or "").strip(),
                        "candidates": [
                            {
                                "id": p.pk,
                                "label": p.name,
                                "player_number": p.player_number,
                                "rating": p.rating,
                            }
                            for p in existing
                        ],
                    },
                    status=409,
                )

        player, error = self._create(request, name, data)
        if error:
            return JsonResponse({"error": error}, status=400)
        return JsonResponse({
            "ok": True,
            "id": player.pk,
            "label": player.name,
            "rating": player.rating,
            "player_number": player.player_number,
        })

    def _create(self, request, name, data):
        """Create the player through the ``player_created`` command.

        Players are global, so there is no single tournament they belong to —
        but a creation still has to be *somewhere* in the log or a replay into a
        fresh database cannot rebuild them with their real number and ratings.
        This endpoint is reached from a division's entrants grid, so the event
        goes in that tournament's log; if the caller does not say which, the
        player is created directly and the roster events still carry enough to
        reconstruct them.
        """
        from tournaments.models import next_temp_player_number

        name = (name or "").strip()
        if not name:
            return None, "Name is required."
        try:
            rating = int(data.get("rating") or 0)
        except (ValueError, TypeError):
            rating = 0

        division = self._division_from(data)
        number = next_temp_player_number()
        payload = {
            "player_number": number,
            "name": name,
            "rating": rating,
            "wespa_rating": data.get("wespa_rating"),
        }
        if division is None:
            return Player.create(name=name, rating=rating)
        actor = request.user if request.user.is_authenticated else None
        return create_player(division.tournament, actor, payload), None

    @staticmethod
    def _division_from(data):
        """The division whose grid asked for this player, if it said."""
        slugs = (data.get("tournament_slug"), data.get("division_slug"))
        if not all(slugs):
            return None
        return Division.objects.filter(
            tournament__slug=slugs[0], slug=slugs[1]
        ).first()


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


class RosterImportView(LoginRequiredMixin, IsAdminMixin, View):
    """Admin-only page to pull the central roster from a snapshot file.

    The "before" half of the registry sync: once this has run, Baxter can run a
    whole tournament with no connection to the central database
    (``plans/PLAN_COCO_PROGRAM.md``). The authenticated endpoint is the normal
    path and the file is the offline one, but both produce the same document and
    ``import_roster`` is one code path — so this page will keep working
    unchanged when the endpoint arrives.

    Global and unlogged, like the other roster imports, and for the same reason:
    entrants freeze their rating seed when they enter, so a pull cannot move a
    tournament that is already under way.
    """

    template_name = "tournaments/roster_import.html"

    def _context(self):
        return {
            "player_count": Player.objects.filter(is_bye=False).count(),
            "provisional_count": Player.objects.filter(
                is_bye=False, is_provisional=True
            ).count(),
        }

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        uploaded = request.FILES.get("roster_file")
        if not uploaded:
            messages.error(request, "No file uploaded.")
            return redirect("roster_import")
        try:
            result = import_roster(uploaded.read())
        except RosterParseError as exc:
            messages.error(request, str(exc))
            return redirect("roster_import")

        stamp = f" (generated {result.generated_at})" if result.generated_at else ""
        messages.success(
            request,
            f"Pulled {result.total} player(s){stamp}: {len(result.added)} added, "
            f"{len(result.updated)} updated, {len(result.unchanged)} unchanged.",
        )
        return redirect("roster_import")


class WespaImportView(LoginRequiredMixin, IsAdminMixin, View):
    """Admin-only page to refresh WESPA ratings from a CSV upload.

    Global and unlogged, like ``PlayerImportView``, and for a reason worth
    stating: entrants pin their rating when they enter (PLAN_ENTRANTS decision
    3), so refreshing the roster mutates no replayable tournament state and
    cannot reshuffle a division that is already under way.
    """

    template_name = "tournaments/wespa_import.html"

    def _context(self):
        return {
            "player_count": Player.objects.filter(is_bye=False).count(),
            "wespa_count": Player.objects.filter(
                is_bye=False, wespa_rating__isnull=False
            ).count(),
        }

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        uploaded = request.FILES.get("wespa_file")
        if not uploaded:
            messages.error(request, "No file uploaded.")
            return redirect("wespa_import")

        rows, errors = parse_wespa_csv(uploaded.read().decode("utf-8-sig"))
        if errors:
            for error in errors[:25]:
                messages.error(request, error)
            if len(errors) > 25:
                messages.error(request, f"... and {len(errors) - 25} more.")
            return redirect("wespa_import")

        result = refresh_wespa_ratings(rows)
        messages.success(
            request,
            f"{result.total} row(s): {len(result.updated)} updated, "
            f"{len(result.unchanged)} unchanged.",
        )
        # Unresolved rows are warnings, not failures — the rest applied. They are
        # listed rather than counted, because "3 names were ambiguous" is not
        # something anyone can act on.
        for name in result.ambiguous[:25]:
            messages.warning(
                request, f"{name} — more than one player has that name; skipped."
            )
        for name in result.unmatched[:25]:
            messages.warning(request, f"{name} — no such player; skipped.")
        return redirect("wespa_import")


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
                "first_player": pairing.first.key,
                "second_player": pairing.second.key,
                "winner_player": winner.key,
                "winner_score": form.cleaned_data["winner_score"],
                "loser_score": form.cleaned_data["loser_score"],
            }
            actor = request.user if request.user.is_authenticated else None
            # Attribute anonymous submissions to a browser (hashed), not a user.
            from tournaments.events import hashed_session

            actor_session = "" if actor else hashed_session(request)
            command = add_result if creating else edit_result
            rs = command(
                division.tournament, actor, payload, actor_session=actor_session
            )
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

    def on_saved(self, division, rows):
        # The grid takes the start from a column the director types, so it is the
        # one path that can contradict a published board. Correct it *after* the
        # save event, so the log holds what was entered and then what it was
        # rewritten to, in that order — which is also the order a replay applies
        # them in.
        from tournaments.starts import correct_result_starts

        super().on_saved(division, rows)
        actor = self.request.user if self.request.user.is_authenticated else None
        correct_result_starts(division.tournament, actor, {"division": division.name})


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
        # The client posts player numbers; the field names stay "first"/"second"
        # because they are positions on a board, not names.
        first_key = data.get("first")
        second_key = data.get("second")
        if not all([round_num, first_key, second_key]):
            return JsonResponse({"error": "Missing required fields."}, status=400)

        error = _require_published_round(division, round_num)
        if error:
            return error

        entrants = {
            e.key: e
            for e in division.entrants.select_related("player")
        }
        first_entrant = entrants.get(first_key)
        second_entrant = entrants.get(second_key)
        if not first_entrant or not second_entrant:
            return JsonResponse({"error": "Entrant not found."}, status=400)

        simulate_match_cmd(
            division.tournament, request.user,
            {"division": division.name, "round": round_num,
             "first_player": first_key, "second_player": second_key},
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


# ---------------------------------------------------------------------------
# Playoffs
# ---------------------------------------------------------------------------


def _series_row(series, names):
    """One series flattened for the bracket template.

    A ``Series`` carries participant *keys*; the template needs names to show.
    The "who won" highlights are resolved here, on the keys, rather than left to
    the template to rederive by comparing the rendered strings — two players in
    one bracket may share a name, and that comparison would light up both rows.
    """

    def name(key):
        return names.get(key, key) if key else key

    return {
        "label": series.label,
        "status": series.status,
        "max_games": series.max_games,
        "high": name(series.high),
        "low": name(series.low),
        "high_score": series.high_score,
        "low_score": series.low_score,
        "high_spread": series.high_spread,
        "high_won": series.winner is not None and series.winner == series.high,
        "low_won": series.winner is not None and series.winner == series.low,
        "winner": name(series.winner),
        "decided_by": series.decided_by,
        "games": [
            {
                "number": g.number,
                "round": g.round,
                "status": g.status,
                "played": g.played,
                "tied": g.tied,
                "high_score": g.high_score,
                "low_score": g.low_score,
                "winner": name(g.winner),
            }
            for g in series.games
        ],
    }


def _playoff_context(division, playoff):
    """Bracket, placements and headline state for a division's playoff pages."""
    pd = PairingData.for_division(division)
    bracket = build_bracket(playoff.config(), pd.result_slips)
    standings = standings_after_round(
        pd, division.max_round(), include_dropped=True
    )
    numbers = {
        e.player.player_number: e.number
        for e in division.entrants.select_related("player")
    }
    # Disambiguate first: the bracket, the placements and the qualifiers table
    # all take their names from these rows.
    label_standings(division, standings)
    names = {p.key: p.name for p in standings}
    placements = final_placements(bracket, standings, numbers)
    # Group the series into their windows so the template can render the bracket
    # column by column, which is how a bracket is read.
    windows = [
        {
            "index": window.index,
            "rounds": list(window.rounds),
            "series": [
                _series_row(s, names)
                for s in bracket.series
                if s.window == window.index
            ],
        }
        for window in bracket.windows
    ]
    return {
        "division": division,
        "playoff": playoff,
        "bracket": bracket,
        "windows": windows,
        "placements": placements,
        "bracket_placements": placements[: playoff.qualifier_count],
        "field_placements": placements[playoff.qualifier_count :],
        # The recorded snapshot's stored name may predate a later clash, so show
        # the same label the rest of the page uses.
        "seeds": [
            {**seed, "player": names.get(seed.get("key"), seed.get("player"))}
            for seed in playoff.seeds
        ],
    }


class DivisionPlayoffView(DivisionNavMixin, VisibleDivisionMixin, DetailView):
    """The public bracket: every series with its length, score, status, games and
    where its winner and loser go, plus the bracket-derived final placements."""

    model = Division
    template_name = "tournaments/division_playoff.html"
    context_object_name = "division"
    active_tab = "playoff"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        playoff = playoff_for(self.object)
        if playoff is None:
            context["no_playoff"] = True
            return context
        context.update(_playoff_context(self.object, playoff))
        context["active_tab"] = self.active_tab
        context["can_edit"] = self.object.tournament.can_edit(self.request.user)
        return context


class PlayoffSetupView(LoginRequiredMixin, DivisionNavMixin, CanEditDivisionMixin, View):
    """Configure a playoff: pick the qualification round, size, timing and series
    lengths, preview the qualifiers the standings give, then confirm.

    The preview is a plain re-render on change (no JS needed): the director
    submits ``preview`` to refresh the qualifier list, and ``confirm`` to create
    the playoff. Seeds are editable before confirming, because the standings can
    tie exactly and only a human can settle that.
    """

    template_name = "tournaments/division_playoff_setup.html"

    def get(self, request, *args, **kwargs):
        division = self.get_division()
        return render(request, self.template_name, self._context(division))

    def post(self, request, *args, **kwargs):
        division = self.get_division()
        data = request.POST
        if data.get("action") == "delete":
            return self._delete(request, division)
        form = self._read_form(division, data)
        if data.get("action") == "confirm":
            errors = self._save(request, division, form)
            if not errors:
                messages.success(request, "Playoff created.")
                return redirect("division_playoff", **division.slug_kwargs())
            form["errors"] = errors
        return render(request, self.template_name, self._context(division, form))

    # -- helpers ---------------------------------------------------------

    def _delete(self, request, division):
        if self._has_results(division):
            messages.error(
                request,
                "This playoff already has results — delete those first.",
            )
        else:
            delete_playoff(
                division.tournament, request.user, {"division": division.name}
            )
            messages.success(request, "Playoff removed.")
        return redirect("division_playoff_setup", **division.slug_kwargs())

    def _has_results(self, division):
        """Whether any playoff game has been played. A playoff can be
        reconfigured or removed freely until then, and not afterwards."""
        playoff = playoff_for(division)
        if playoff is None:
            return False
        rounds = list(playoff.bracket().rounds)
        return division.result_slips.filter(round__in=rounds).exists()

    def _read_form(self, division, data):
        """Pull the submitted configuration out of the POST, falling back to
        sensible defaults so a half-filled form still renders."""
        try:
            count = int(data.get("qualifier_count") or 4)
        except ValueError:
            count = 4
        if count not in QUALIFIER_COUNTS:
            count = 4
        try:
            qualification_round = int(data.get("qualification_round") or 0)
        except ValueError:
            qualification_round = 0
        timing = data.get("timing") or str(Timing.POSTSCRIPT)
        stage_games = {}
        for key in series_keys(count):
            raw = data.get(f"games_{key}")
            if raw is None:
                stage_games[key] = default_stage_games(count).get(key, 3)
                continue
            try:
                stage_games[key] = int(raw)
            except ValueError:
                stage_games[key] = 0
        # Concurrent mode plays every placement series: an eliminated bracket
        # player with nothing to play would be the only idle person in the room.
        if timing == str(Timing.CONCURRENT):
            for key in placement_keys(count):
                if stage_games.get(key, 0) < 1:
                    stage_games[key] = max(
                        stage_games.get(key, 0), default_stage_games(count)[key]
                    )
        # The override selects come back as player keys, not names — the
        # option values are keys precisely so a shared name cannot pick the
        # wrong person.
        seed_keys = data.getlist("seed")
        return {
            "qualification_round": qualification_round,
            "qualifier_count": count,
            "timing": timing,
            "stage_games": stage_games,
            "seed_keys": [k for k in seed_keys if k],
            "errors": [],
        }

    def _seeds(self, division, form):
        """The seed snapshot to offer: the director's override if they supplied
        one, else the standings at the qualification round."""
        auto = qualification_seeds(
            division, form["qualification_round"], form["qualifier_count"]
        )
        keys = form.get("seed_keys") or []
        if len(keys) != form["qualifier_count"]:
            return auto
        # An override may name anyone in the standings, not just the automatic
        # top N, so fall back to the whole field for the name and record.
        everyone = {
            s["key"]: s
            for s in qualification_seeds(
                division, form["qualification_round"], count=None
            )
        }
        return [
            {
                "seed": i + 1,
                "key": key,
                "player": everyone.get(key, {}).get("player", key),
                "wins": everyone.get(key, {}).get("wins", 0),
                "spread": everyone.get(key, {}).get("spread", 0),
            }
            for i, key in enumerate(keys)
        ]

    def _save(self, request, division, form):
        from tournaments.playoff import PlayoffConfig

        seeds = self._seeds(division, form)
        config = PlayoffConfig(
            qualification_round=form["qualification_round"],
            qualifier_count=form["qualifier_count"],
            timing=form["timing"],
            stage_games=form["stage_games"],
            seeds=tuple(s["key"] for s in seeds),
        )
        errors = validate_config(config) + schedule_conflicts(division, config)
        if errors:
            return errors
        payload = {
            "division": division.name,
            "qualification_round": config.qualification_round,
            "qualifier_count": config.qualifier_count,
            "timing": config.timing,
            "stage_games": config.stage_games,
            "seeds": seeds,
        }
        command = update_playoff if playoff_for(division) else create_playoff
        try:
            command(division.tournament, request.user, payload)
        except ValueError as exc:
            return [str(exc)]
        return []

    def _context(self, division, form=None):
        playoff = playoff_for(division)
        if form is None:
            if playoff is not None:
                form = {
                    "qualification_round": playoff.qualification_round,
                    "qualifier_count": playoff.qualifier_count,
                    "timing": playoff.timing,
                    "stage_games": playoff.stage_games,
                    "seed_keys": [s["key"] for s in playoff.seeds],
                    "errors": [],
                }
            else:
                count = 4
                form = {
                    # Default to where a postscript playoff has to qualify:
                    # the last configured round, played or not.
                    "qualification_round": (
                        max(selectable_qualification_rounds(division), default=1)
                    ),
                    "qualifier_count": count,
                    "timing": str(Timing.POSTSCRIPT),
                    "stage_games": default_stage_games(count),
                    "seed_keys": [],
                    "errors": [],
                }
        count = form["qualifier_count"]
        # Candidates for a manual override: everyone still standing, best first.
        pd = PairingData.for_division(division)
        field = standings_after_round(pd, form["qualification_round"])
        label_standings(division, field)
        candidates = [{"key": p.key, "name": p.name} for p in field]
        return {
            "division": division,
            "active_tab": "playoff",
            "can_edit": True,
            "form": form,
            "playoff": playoff,
            "has_results": self._has_results(division),
            "seeds": self._seeds(division, form),
            "candidates": candidates,
            "qualifier_counts": QUALIFIER_COUNTS,
            "timings": [
                (str(Timing.POSTSCRIPT), "Postscript — the main tournament ends "
                 "at the qualification round"),
                (str(Timing.CONCURRENT), "Concurrent — everyone else keeps "
                 "playing the configured schedule"),
            ],
            "series_rows": [
                {
                    "key": key,
                    "label": SERIES_LABELS[key],
                    "games": form["stage_games"].get(key, 0),
                    "optional": key in placement_keys(count),
                }
                for key in series_keys(count)
            ],
            "errors": form.get("errors", []),
            # Every configured round, not just the played ones — a postscript
            # playoff qualifies on the last round of the schedule, which is
            # normally still unplayed when the director sets the playoff up.
            "rounds": selectable_qualification_rounds(division) or [1],
        }
