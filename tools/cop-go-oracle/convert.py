"""Convert a liwords COP ``PairRequest`` into Baxter's engine input.

The oracle speaks liwords' format (``api/proto/ipc/pair.proto`` upstream, as
protobuf JSON); Baxter's Rust engine takes a ``PairingInput``
(``scrabble-pairing/src/model.rs``) through ``scrabble_pairing_py.pair_json``.
This maps one onto the other so the same position can be paired by both.

    python convert.py < request.json > input.json     # warnings on stderr

The mapping, and where it is lossy:

- **Players** are the request's names, in index order. Past round 0 the engine
  seeds nobody off ratings, but a rating is still required, so each player gets
  a descending one (``len - index``): index order is the only seeding liwords
  has. ``removedPlayers`` become ``dropped``.
- **Results.** liwords stores each player's *score* per round and a pairing
  array of opponent indexes; the engine wants one slip per game. A game becomes
  a slip, higher score winning (equal scores are a draw either way round).
  liwords records no start, so every slip says the loser went first.
- **Byes** (a player paired with themselves) score as liwords scores them: a
  positive score is a win by that spread, a negative one a loss by it, and zero
  a *draw* — liwords derives a record from rounds played, so a round with
  neither a win nor a loss counts half. All three become slips against the
  engine's ``"Bye"`` (the zero one 0–0).
- **The round to pair** is the first without results. A last round that is
  already partly paired (``-1`` for the unpaired) pins its existing games as
  ``fixed_pairings``, which is how liwords treats them (prepaired players).
- **Schedule.** Every round ``1..rounds`` is COP, each paired from the round
  before: the engine takes the total round count from the schedule, and treats
  rounds with results as history.
- **Config.** ``gibsonSpread``, ``hopefulnessThreshold`` and
  ``controlLossThreshold`` are single values upstream and one-element arrays
  here (the engine forward-fills them). Both count control-loss activation in
  completed rounds, so it maps as is. ``divisionSims``/``controlLossSims`` are
  the engine's ``simulations``/``always_wins_simulations`` — but upstream
  *re-simulates* past ``divisionSims`` until a hopefulness threshold is met, and
  Baxter runs exactly the count it is given; that difference is the port's, not
  the converter's.

- **Seed.** liwords seeds COP from ``seed``, or when that is 0 from a 64-bit
  FNV-1 hash of the concatenated names plus the number of rounds in
  ``divisionPairings``. The engine seeds a COP round from ``seed + round - 1``,
  so the converter writes upstream's seed minus that offset: the round then
  runs on exactly upstream's seed.

**Not expressible** in the engine, and reported as warnings so a comparison can
mark the case rather than blame the port: class prizes with anyone in a class
(Baxter defers them), ``topDownByes``, and any pairing method other than COP.
(``factor`` and ``initialNonperfRounds`` only steer upstream's non-COP methods,
so a COP request carrying them is still comparable.)
"""

import json
import sys
from dataclasses import dataclass, field

BYE = "Bye"  # scrabble-pairing's standings::BYE_NAME


@dataclass
class Conversion:
    """The engine input, plus what the request asked for that it cannot say."""

    engine_input: dict
    round: int  # the round the engine should pair (1-indexed)
    names: list  # index -> name, for mapping the engine's answer back
    warnings: list = field(default_factory=list)

    @property
    def comparable(self):
        """Whether the two engines were asked the same question."""
        return not self.warnings


def _config(req, warnings):
    unsupported = {
        "topDownByes": "top-down byes",
    }
    for key, what in unsupported.items():
        if req.get(key):
            warnings.append(f"uses {what} ({key}={req[key]!r}), which the engine has no equivalent of")
    # Class k competes for classPrizes[k - 1]; class 0 is no class. So prizes
    # with nobody in a class are inert, and only a classed player is a warning.
    classed = sum(1 for c in req.get("playerClasses") or [] if c > 0)
    if classed and req.get("classPrizes"):
        warnings.append(
            f"uses class prizes ({classed} player(s) in a class), which Baxter defers"
        )
    method = req.get("pairMethod", "COP")
    if method not in ("COP", 0):
        warnings.append(f"asks for pairing method {method!r}, not COP")
    return {
        "place_prizes": req.get("placePrizes", 0),
        "gibson_spreads": [req.get("gibsonSpread", 0)],
        "hopefulness": [req.get("hopefulnessThreshold", 0.0)],
        "control_loss_thresholds": [req.get("controlLossThreshold", 0.0)],
        "control_loss_activation_round": req.get("controlLossActivationRound", 0),
        "simulations": req.get("divisionSims", 0),
        "always_wins_simulations": req.get("controlLossSims", 0),
        "disallow_repeat_byes": not req.get("allowRepeatByes", False),
    }


def convert(req):
    """``req`` is the parsed PairRequest JSON. Returns a :class:`Conversion`."""
    warnings = []
    names = list(req.get("playerNames") or [])
    if len(set(names)) != len(names):
        # The engine keys players by name; two with one name would merge.
        raise ValueError("player names are not unique")
    if BYE in names:
        raise ValueError(f"a player is named {BYE!r}, the engine's bye")
    removed = set(req.get("removedPlayers") or [])
    players = [
        {"name": name, "rating": len(names) - i, "dropped": i in removed}
        for i, name in enumerate(names)
    ]

    pairings = [r.get("pairings") or [] for r in req.get("divisionPairings") or []]
    results = [r.get("results") or [] for r in req.get("divisionResults") or []]

    slips = []
    for round_idx, scores in enumerate(results):
        opps = pairings[round_idx]
        for i, opp in enumerate(opps):
            if opp < 0:
                continue
            if opp == i:
                score = scores[i]
                if i in removed:
                    # liwords parks a withdrawn player on themselves each round.
                    continue
                if score >= 0:
                    slips.append(_slip(round_idx + 1, names[i], BYE, score, 0))
                else:
                    slips.append(_slip(round_idx + 1, BYE, names[i], -score, 0))
                continue
            if i < opp:
                a, b = scores[i], scores[opp]
                winner, loser = (i, opp) if a >= b else (opp, i)
                slips.append(_slip(
                    round_idx + 1, names[winner], names[loser],
                    scores[winner], scores[loser],
                ))

    to_pair = len(results) + 1
    fixed = {}
    if len(pairings) > len(results):
        # The round being paired is already partly paired: those games stand.
        pinned = []
        for i, opp in enumerate(pairings[len(results)]):
            if i in removed:
                # liwords parks a withdrawn player on themselves; not a bye.
                continue
            if opp == i:
                pinned.append([names[i], BYE])
            elif opp > i:
                pinned.append([names[i], names[opp]])
        if pinned:
            fixed[str(to_pair)] = pinned

    rounds = req.get("rounds", 0)
    if to_pair > rounds:
        warnings.append(f"every round is already paired (round {to_pair} of {rounds})")

    engine_input = {
        "players": players,
        "result_slips": slips,
        "round_pairings": [
            {"round": r, "pairing": "COP", "start_round": r - 1}
            for r in range(1, rounds + 1)
        ],
        "cop_config": _config(req, warnings),
        "fixed_pairings": fixed,
        "seed": (upstream_seed(req) - (to_pair - 1)) & _U64,
    }
    return Conversion(engine_input, to_pair, names, warnings)


_U64 = 0xFFFF_FFFF_FFFF_FFFF


def upstream_seed(req):
    """The seed liwords' COPPair runs on, as the uint64 its PCG is seeded with.

    ``seed`` when set (an int64 in the request; negative wraps); otherwise Go's
    ``fnv.New64`` (FNV-1, not 1a) over the names back to back, plus the number
    of rounds in ``divisionPairings`` — cop.go's ``COPPair``.
    """
    seed = int(req.get("seed") or 0)
    if seed:
        return seed & _U64
    h = 0xCBF29CE484222325
    for name in req.get("playerNames") or []:
        for byte in name.encode():
            h = (h * 0x100000001B3) & _U64
            h ^= byte
    return (h + len(req.get("divisionPairings") or [])) & _U64


def _slip(round, winner, loser, winner_score, loser_score):
    return {
        "round": round,
        "winner_name": winner,
        "loser_name": loser,
        "winner_score": winner_score,
        "loser_score": loser_score,
        # liwords records no start; the engine needs one, and COP ignores it.
        "winner_started": False,
    }


def main():
    conversion = convert(json.load(sys.stdin))
    for warning in conversion.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    json.dump(conversion.engine_input, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
