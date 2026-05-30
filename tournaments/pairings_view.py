"""Presenter for the division pairings page.

Owns a single ``PairingData`` per render, caches the derived querysets,
and exposes the context fields needed by ``DivisionPairingsView``, the
Datastar fragment endpoint ``RoundPairingsTabView``, and the public
``PublishedPairingsView``.

Tab status is read from ``RoundPairings.status`` (the DB lifecycle field);
result slips are only consulted to decorate per-pairing rows, never to
infer round-level state.
"""

from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from itertools import groupby

from .models import Pairing, RoundPairings
from .pairing.base import PairingData
from .pairing.round_pairing import RP


@dataclass(frozen=True)
class AnnotatedPairing:
    """A division Pairing decorated with its result and fixed-status flags."""

    pairing: Pairing
    result: str = ""
    is_fixed: bool = False


class RoundTabStatus(Enum):
    """Status of a round tab on the pairings page.

    Distinct from ``RoundPairings.STATUS_CHOICES`` (the lifecycle status):
    a tab can be FUTURE/PAIRABLE/ERROR_NO_PAIRINGS even without any
    corresponding RoundPairings row.
    """

    FINISHED = "finished"
    IN_PROGRESS = "in_progress"
    PUBLISHED = "published"
    PAIRABLE = "pairable"
    FUTURE = "future"
    ERROR_NO_PAIRINGS = "error_no_pairings"

    @property
    def css_class(self) -> str:
        return self.value

    @property
    def badge_class(self) -> str:
        return _BADGE_CLASSES[self]

    @property
    def badge_label(self) -> str:
        return _BADGE_LABELS[self]


_BADGE_CLASSES = {
    RoundTabStatus.FINISHED: "badge-finished",
    RoundTabStatus.IN_PROGRESS: "badge-in-progress",
    RoundTabStatus.PUBLISHED: "badge-published",
    RoundTabStatus.PAIRABLE: "badge-pairable",
    RoundTabStatus.FUTURE: "badge-future",
    RoundTabStatus.ERROR_NO_PAIRINGS: "badge-error",
}

_BADGE_LABELS = {
    RoundTabStatus.FINISHED: "Finished",
    RoundTabStatus.IN_PROGRESS: "In Progress",
    RoundTabStatus.PUBLISHED: "Published",
    RoundTabStatus.PAIRABLE: "Pairable",
    RoundTabStatus.FUTURE: "Future",
    RoundTabStatus.ERROR_NO_PAIRINGS: "error: results but no pairing",
}


class PairingsPresenter:
    """View-state for the division pairings page."""

    def __init__(self, division):
        self.division = division
        self._explicit_selected: int | None = None

    def select(self, round_num):
        """Override the default selected round (used by the fragment endpoint)."""
        self._explicit_selected = int(round_num)
        for attr in ("selected_round", "selected_tab"):
            self.__dict__.pop(attr, None)
        return self

    # --- Cached data sources ---

    @cached_property
    def pd(self) -> PairingData:
        return PairingData.for_division(self.division)

    @cached_property
    def db_status_map(self) -> dict[int, str]:
        return dict(self.division.round_pairings_set.values_list("round", "status"))

    def _can_pair(self, rp) -> bool:
        """Whether the settings entry's round can currently be (re)paired.

        Mirrors ``pairing.pair.can_pair`` but reads the DB lifecycle status
        from ``db_status_map`` instead of inferring from result slips.
        """
        db_status = self.db_status_map.get(rp.round)
        if db_status in (
            RoundPairings.PUBLISHED,
            RoundPairings.IN_PROGRESS,
            RoundPairings.FINISHED,
        ):
            return False
        if RP.is_round_robin(rp.pairing):
            return True
        if rp.start_round == 0:
            return True
        return self.db_status_map.get(rp.start_round) == RoundPairings.FINISHED

    @cached_property
    def rounds_with_pairings(self) -> set[int]:
        return set(self.division.pairings.values_list("round", flat=True).distinct())

    @cached_property
    def rounds_with_results(self) -> set[int]:
        return set(self.division.result_slips.values_list("round", flat=True).distinct())

    @cached_property
    def db_pairings(self):
        return list(
            self.division.pairings
            .select_related("first", "first__player", "second", "second__player")
            .order_by("round", "table")
        )

    @cached_property
    def played(self):
        return {
            (slip.round, frozenset({slip.winner_id, slip.loser_id})): slip
            for slip in self.division.result_slips.all()
        }

    @cached_property
    def fixed_lookup(self):
        return {
            (fp.round_number, frozenset({fp.entrant1_id, fp.entrant2_id}))
            for fp in self.division.fixed_pairings.all()
        }

    @cached_property
    def fixed_for_selected(self):
        if self.selected_round is None:
            return []
        return list(
            self.division.fixed_pairings
            .filter(round_number=self.selected_round)
            .select_related("entrant1__player", "entrant2__player")
        )

    @cached_property
    def has_draft_rounds(self) -> bool:
        return self.division.round_pairings_set.filter(status=RoundPairings.DRAFT).exists()

    @cached_property
    def has_published_rounds(self) -> bool:
        return self.division.round_pairings_set.filter(
            status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS]
        ).exists()

    # --- Tab derivation ---

    @cached_property
    def tabs(self):
        if not self.pd.round_pairings:
            return []
        return [self._build_tab(rp) for rp in self.pd.round_pairings]

    def _build_tab(self, rp):
        r = rp.round
        db_status = self.db_status_map.get(r)
        if db_status == RoundPairings.FINISHED:
            tab_status = RoundTabStatus.FINISHED
        elif db_status == RoundPairings.IN_PROGRESS:
            tab_status = RoundTabStatus.IN_PROGRESS
        elif db_status == RoundPairings.PUBLISHED:
            tab_status = RoundTabStatus.PUBLISHED
        elif self._can_pair(rp):
            tab_status = RoundTabStatus.PAIRABLE
        else:
            tab_status = RoundTabStatus.FUTURE
        # Defensive: any round with slips but no Pairings is in a broken state.
        # Post edit-results validation, this only happens via direct DB writes
        # (e.g. the import_results management command, or shell sessions).
        if r in self.rounds_with_results and r not in self.rounds_with_pairings:
            tab_status = RoundTabStatus.ERROR_NO_PAIRINGS
        label = rp.pairing
        if rp.start_round:
            label += f" (from round {rp.start_round})"
        return {
            "round": r,
            "status": tab_status.value,
            "label": label,
            "_enum": tab_status,
        }

    # --- Selected round ---

    @cached_property
    def selected_round(self):
        if self._explicit_selected is not None:
            return self._explicit_selected
        tabs = self.tabs
        if not tabs:
            return None
        for preference in ("in_progress", "published", "pairable"):
            for tab in tabs:
                if tab["status"] == preference:
                    return tab["round"]
        finished = [t for t in tabs if t["status"] == "finished"]
        if finished:
            return finished[-1]["round"]
        return tabs[0]["round"]

    @cached_property
    def selected_tab(self):
        if self.selected_round is None:
            return None
        return next((t for t in self.tabs if t["round"] == self.selected_round), None)

    # --- Row builders ---

    def _annotate(self, round_pairings, round_num):
        rows = []
        for p in round_pairings:
            key = (round_num, frozenset({p.first_id, p.second_id}))
            slip = self.played.get(key)
            if slip:
                scores = {slip.winner_id: slip.winner_score, slip.loser_id: slip.loser_score}
                result = f"{scores[p.first_id]} - {scores[p.second_id]}"
            else:
                result = ""
            rows.append(AnnotatedPairing(
                pairing=p,
                result=result,
                is_fixed=key in self.fixed_lookup,
            ))
        return rows

    def _rows_for_selected(self):
        tab = self.selected_tab
        if tab is None:
            return None
        round_num = tab["round"]
        status = tab["status"]
        if status not in ("finished", "in_progress", "pairable", "published"):
            return None  # 'future' or 'error_no_pairings' — body shows a message.
        round_pairings = [p for p in self.db_pairings if p.round == round_num]
        if not round_pairings:
            return None
        return self._annotate(round_pairings, round_num)

    # --- Edit-only fields ---

    @cached_property
    def available_rounds(self):
        return [rp.round for rp in self.pd.round_pairings if self._can_pair(rp)]

    @cached_property
    def waiting_message(self) -> str | None:
        if not self.pd.round_pairings:
            return "No round pairings configured."
        for rp in self.pd.round_pairings:
            db_status = self.db_status_map.get(rp.round)
            if db_status in (RoundPairings.FINISHED, RoundPairings.IN_PROGRESS):
                continue
            if rp.start_round and self.db_status_map.get(rp.start_round) != RoundPairings.FINISHED:
                return f"Round {rp.round} is waiting for round {rp.start_round} results."
            return None
        return "All rounds are finished."

    @cached_property
    def rounds_needing_generation(self) -> list[int]:
        """Pairable rounds that have no pairings yet (need auto-generation)."""
        return [r for r in self.available_rounds if r not in self.rounds_with_pairings]

    # --- Context builders ---

    def as_context(self) -> dict:
        """Context dict for the full page and the round-tab fragment."""
        public_tabs = [
            {k: v for k, v in t.items() if not k.startswith("_")} for t in self.tabs
        ]
        context = {"division": self.division, "round_tabs": public_tabs}
        if not self.tabs:
            context["pairings_message"] = "No round pairings configured."
            return context
        context["selected_round"] = self.selected_round
        sel = self.selected_tab
        if sel:
            context["selected_status"] = sel["status"]
            context["selected_status_badge_class"] = sel["_enum"].badge_class
            context["selected_status_badge_label"] = sel["_enum"].badge_label
            context["round_label"] = sel["label"]
            if sel["status"] == "pairable":
                context["fixed_pairings_for_round"] = self.fixed_for_selected
        rows = self._rows_for_selected()
        if rows is not None:
            context["round_pairings"] = rows
        context["has_draft_rounds"] = self.has_draft_rounds
        context["has_published_rounds"] = self.has_published_rounds
        return context


class PublishedPairingsPresenter:
    """View-state for the public published-pairings page (no tabs, no controls)."""

    def __init__(self, division):
        self.division = division

    def as_context(self) -> dict:
        context = {"division": self.division}
        published_rounds = set(
            self.division.round_pairings_set
            .filter(status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS])
            .values_list("round", flat=True)
        )
        if not published_rounds:
            context["pairings_message"] = "No pairings published yet."
            return context
        db_pairings = list(
            self.division.pairings
            .filter(round__in=published_rounds)
            .select_related("first", "first__player", "second", "second__player")
            .order_by("round", "table")
        )
        if not db_pairings:
            context["pairings_message"] = "No pairings published yet."
            return context
        annotated = []
        for round_num, round_pairings in groupby(db_pairings, key=lambda p: p.round):
            annotated.append(
                (round_num, [AnnotatedPairing(pairing=p) for p in round_pairings])
            )
        context["pairings"] = annotated
        return context
