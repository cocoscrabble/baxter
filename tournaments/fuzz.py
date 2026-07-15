"""Seeded tournament fuzzer for simulation testing.

Drives a tournament through random-but-valid operations against the real views
(so every accepted op is a logged event), checking invariants after each step —
including the meta-invariant that replaying the log so far into a fresh database
reproduces the state digest. A failing run's artifact is its event log.

Used by the fixed-seed tests (CI) and the ``fuzz_tournament`` command.
"""

import json
import random

from django.test import Client

from tournaments.events import division_digest
from tournaments.models import Player, RoundPairings, Tournament
from tournaments.replay import events_from_tournament, replay
from users.models import User


class InvariantError(AssertionError):
    pass


class Fuzzer:
    def __init__(self, seed, *, n_players=8):
        self.rng = random.Random(seed)
        self.seed = seed
        self.client = Client()
        self.n_players = n_players
        self.tournament = None
        self.division = None
        self.players = []

    # -- setup ------------------------------------------------------------

    def setup(self):
        self.owner = User.objects.create_user(
            username=f"fuzz_owner_{self.seed}", password="pw"
        )
        self.client.force_login(self.owner)
        self.players = [
            Player.objects.create(
                name=f"P{i:02d}_{self.seed}",
                player_number=f"{self.seed}{i:03d}",
                rating=2000 - 10 * i,
            )
            for i in range(self.n_players)
        ]
        from django.urls import reverse

        self.client.post(
            reverse("tournament_create"),
            {"name": f"Fuzz {self.seed}", "location": "X", "start_date": "2026-03-15",
             "editor_usernames": ""},
        )
        self.tournament = Tournament.objects.get(name=f"Fuzz {self.seed}")
        # A test division so simulate ops are available.
        self.client.post(
            reverse("division_create", kwargs={"tournament_slug": self.tournament.slug}),
            {"name": "Div", "is_test": "1"},
        )
        self.division = self.tournament.divisions.get(name="Div")

    # -- helpers ----------------------------------------------------------

    def _url(self, name, **extra):
        from django.urls import reverse

        return reverse(name, kwargs={**self.division.slug_kwargs(), **extra})

    def _post_json(self, name, body):
        return self.client.post(
            self._url(name), json.dumps(body), content_type="application/json"
        )

    # -- operations -------------------------------------------------------

    def op_set_entrants(self):
        k = self.rng.randint(2, self.n_players)
        chosen = self.rng.sample(self.players, k)
        rows = [
            {"number": i + 1, "player": p.pk, "dropped": False}
            for i, p in enumerate(chosen)
        ]
        self._post_json("division_entrants_edit", {"rows": rows})

    def op_save_settings(self):
        # KotH/Swiss handle any round count. Round-robin/quads have field-size
        # constraints (an RR block with more rounds than E-1 overflows the
        # engine — a separate pre-existing bug), so they're left out of the
        # fuzzer's random schedules.
        strategy = self.rng.choice(["KotH", "Swiss"])
        rounds = self.rng.randint(1, 4)
        self._post_json(
            "division_round_pairings",
            {"blocks": [{"pairing": strategy, "rounds": rounds, "pair_from": 1}]},
        )

    def op_pair_rounds(self):
        # Lazily regenerates the draft pairings.
        self.client.get(self._url("division_pair_rounds"))

    def op_publish(self):
        self.op_pair_rounds()
        draft = (
            self.division.round_pairings_set.filter(status=RoundPairings.DRAFT)
            .order_by("round")
            .first()
        )
        if draft is not None:
            self.client.post(self._url("publish_round"), {"round": draft.round})

    def op_simulate_round(self):
        published = (
            self.division.round_pairings_set.filter(
                status__in=[RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS]
            )
            .order_by("round")
            .first()
        )
        if published is not None:
            self._post_json("simulate_round", {"round": published.round})

    def op_drop_entrant(self):
        entrants = list(
            self.division.entrants.select_related("player").order_by("number")
        )
        if len(entrants) < 3:
            return
        target = self.rng.choice(entrants)
        # Can't drop someone who already has results (guarded); skip if so.
        if target.wins.exists() or target.losses.exists():
            return
        rows = [
            {"number": e.number, "player": e.player_id, "dropped": e == target}
            for e in entrants
        ]
        self._post_json("division_entrants_edit", {"rows": rows})

    OPS = [
        (op_set_entrants, 3),
        (op_save_settings, 3),
        (op_pair_rounds, 2),
        (op_publish, 3),
        (op_simulate_round, 3),
        (op_drop_entrant, 1),
    ]

    def step(self):
        ops = [op for op, w in self.OPS for _ in range(w)]
        self.rng.choice(ops)(self)
        self.division.refresh_from_db()

    # -- invariants -------------------------------------------------------

    def check_invariants(self):
        self._inv_no_double_pairing()
        self._inv_round_status_consistent()

    def _inv_no_double_pairing(self):
        for rp in self.division.round_pairings_set.all():
            seen = set()
            for p in rp.pairings.all():
                for eid in (p.first_id, p.second_id):
                    if eid in seen:
                        raise InvariantError(
                            f"entrant {eid} paired twice in round {rp.round}"
                        )
                    seen.add(eid)

    def _inv_round_status_consistent(self):
        for rp in self.division.round_pairings_set.all():
            total = rp.pairings.count()
            with_results = rp.pairings.filter(result__isnull=False).count()
            if rp.status == RoundPairings.FINISHED and total and with_results != total:
                raise InvariantError(
                    f"round {rp.round} FINISHED but {with_results}/{total} results"
                )

    def check_replay_meta_invariant(self):
        events = events_from_tournament(self.tournament)
        digests_before = {
            d.name: division_digest(d) for d in self.tournament.divisions.all()
        }
        ctx = replay(events, verify=True)
        for name, digest in digests_before.items():
            replayed = ctx.tournament.divisions.filter(name=name).first()
            if replayed is None or division_digest(replayed) != digest:
                raise InvariantError(f"replay digest mismatch for division {name}")

    def run(self, steps=25, check_replay_every=None):
        self.setup()
        for i in range(steps):
            self.step()
            self.check_invariants()
            if check_replay_every and (i + 1) % check_replay_every == 0:
                self.check_replay_meta_invariant()
        # Always verify a full replay at the end.
        self.check_replay_meta_invariant()
        return self.tournament
