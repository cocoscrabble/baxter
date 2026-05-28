"""Presenter for the division pairings page.

Owns a single ``PairingData`` per render, caches the derived querysets,
and exposes the context fields needed by ``DivisionPairingsView``, the
Datastar fragment endpoint ``RoundPairingsTabView``, and the public
``PublishedPairingsView``.
"""

from enum import Enum
from functools import cached_property
from itertools import groupby

from .models import RoundPairings
from .pairing.base import PairingData, RoundStatus
from .pairing.pair import can_pair, round_status


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
    def round_statuses(self) -> dict[int, RoundStatus]:
        return round_status(self.pd)

    @cached_property
    def db_status_map(self) -> dict[int, str]:
        return dict(self.division.round_pairings_set.values_list("round", "status"))

    @cached_property
    def rounds_with_pairings(self) -> set[int]:
        return set(self.division.pairings.values_list("round", flat=True).distinct())

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
            (fp.round_number, frozenset({fp.entrant1_id, fp.entrant2_id})): fp.pk
            for fp in self.division.fixed_pairings.all()
        }

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
        elif self.round_statuses.get(r) == RoundStatus.Finished:
            tab_status = RoundTabStatus.FINISHED
        elif self.round_statuses.get(r) == RoundStatus.Partial:
            tab_status = RoundTabStatus.IN_PROGRESS
        elif can_pair(rp, self.round_statuses):
            tab_status = RoundTabStatus.PAIRABLE
        else:
            tab_status = RoundTabStatus.FUTURE
        # In-progress with no Pairing records can't render properly — we have
        # results but don't know the unplayed pairings.
        if tab_status == RoundTabStatus.IN_PROGRESS and r not in self.rounds_with_pairings:
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
            fixed_id = self.fixed_lookup.get(key)
            rows.append({
                "pairing": p,
                "result": result,
                "is_fixed": bool(fixed_id),
                "fixed_id": fixed_id,
            })
        return rows

    def _legacy_slip_rows(self, round_num):
        slips = list(
            self.division.result_slips
            .filter(round=round_num)
            .select_related("winner__player", "loser__player")
            .order_by("pk")
        )
        fp_lookup = {
            frozenset({fp.entrant1_id, fp.entrant2_id})
            for fp in self.division.fixed_pairings.filter(round_number=round_num)
        }
        return [
            {
                "first_name": slip.winner.player.name,
                "second_name": slip.loser.player.name,
                "result": f"{slip.winner_score} - {slip.loser_score}",
                "is_fixed": frozenset({slip.winner_id, slip.loser_id}) in fp_lookup,
                "from_slips": True,
            }
            for slip in slips
        ]

    def _rows_for_selected(self):
        tab = self.selected_tab
        if tab is None:
            return None
        round_num = tab["round"]
        status = tab["status"]
        round_pairings = [p for p in self.db_pairings if p.round == round_num]
        if status in ("finished", "in_progress", "error_no_pairings"):
            if round_pairings:
                return self._annotate(round_pairings, round_num)
            return self._legacy_slip_rows(round_num)
        if status in ("pairable", "published"):
            if round_pairings:
                return self._annotate(round_pairings, round_num)
        return None  # 'future' — template shows settings text instead.

    # --- Edit-only fields ---

    @cached_property
    def available_rounds(self):
        if not self.pd.round_pairings:
            return []
        locked = {
            rp.round for rp in self.pd.round_pairings
            if self.db_status_map.get(rp.round, RoundPairings.DRAFT) != RoundPairings.DRAFT
        }
        return [
            rp.round for rp in self.pd.round_pairings
            if can_pair(rp, self.round_statuses) and rp.round not in locked
        ]

    @cached_property
    def waiting_message(self) -> str | None:
        if not self.pd.round_pairings:
            return "No round pairings configured."
        for rp in self.pd.round_pairings:
            stat = self.round_statuses[rp.round]
            if stat in (RoundStatus.Finished, RoundStatus.Partial):
                continue
            if rp.start_round and self.round_statuses[rp.start_round] != RoundStatus.Finished:
                return f"Round {rp.round} is waiting for round {rp.start_round} results."
            return None
        return "All rounds are finished."

    def generate_label(self) -> str | None:
        rounds = self.available_rounds
        if not rounds:
            return None
        plural = "rounds" if len(rounds) > 1 else "round"
        return f"Generate Pairings ({plural} {', '.join(str(r) for r in rounds)})"

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
            annotated.append((round_num, [{"pairing": p} for p in round_pairings]))
        context["pairings"] = annotated
        return context
