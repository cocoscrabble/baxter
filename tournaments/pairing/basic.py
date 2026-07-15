import random

import more_itertools

from collections import defaultdict

from tournaments.pairing.base import (
    Pairings,
    PairingData,
    PairingError,
    Player,
    guard_no_dropped_in_block,
    pair_no_repeats_blossom,
    standings_after_round,
)
from tournaments.pairing.round_pairing import RoundPairing


# -----------------------------------------------------
# King of the Hill

def pair_koth(pd: PairingData, rp: RoundPairing) -> Pairings:
    """King of the hill pairing."""
    standings = standings_after_round(pd, rp.start_round)
    pairings = Pairings()
    for p1, p2 in more_itertools.chunked(standings, 2):
        pairings.add(p1, p2)
    return pairings


# -----------------------------------------------------
# Queen of the Hill

def pair_qoth(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Queen of the hill pairing."""
    standings = standings_after_round(pd, rp.start_round)
    n = len(standings)
    if n < 4:
        # Too few players for Queen-of-the-Hill groups of four; fall back to KotH.
        return pair_koth(pd, rp)
    pairings = Pairings()
    if n % 4 == 2:
        last = n - 6
        for i in range(0, last, 4):
            pairings.add(standings[i + 0], standings[i + 2])
            pairings.add(standings[i + 1], standings[i + 3])
        # Pair the last six players 1-4,2-5,3-6 if we don't have a
        # multiple of 4
        pairings.add(standings[last + 0], standings[last + 3])
        pairings.add(standings[last + 1], standings[last + 4])
        pairings.add(standings[last + 2], standings[last + 5])
    else:
      for i in range(0, n, 4):
          pairings.add(standings[i + 0], standings[i + 2])
          pairings.add(standings[i + 1], standings[i + 3])
    return pairings


# -----------------------------------------------------
# Round Robin

def _pair_rr(n, r) -> list[list[int]]:
    # Pair n players at round r
    init = [i + 1 for i in range(n - 1)]
    h = n // 2
    start = n - 1 - r
    r1 = init[0: start]
    r2 = init[start:]
    rotated = [0] + r2 + r1
    h1 = rotated[0: h]
    h2 = list(reversed(rotated[h:]))
    return [h1, h2]


def _is_bye_name(name) -> bool:
    return name.lower() == "Bye".lower()


def _rr_players(pd: PairingData):
    """Seeding order with a ``Bye`` appended for odd fields, so the rotation runs
    over an even number of players. Players keep their seeding order; what varies
    is which round-*template* lands in which calendar round (see
    :func:`_rr_permutation`)."""
    players = list(standings_after_round(pd, 0))
    if len(players) % 2 != 0:
        players.append(Player("Bye"))
    return players


def _rr_templates(players):
    """The ``E-1`` round-robin templates over ``E`` (even) players. Returns
    ``(templates, template_of_pair)`` where ``templates[t]`` is the set of
    ``frozenset({name_a, name_b})`` games in template ``t``, and
    ``template_of_pair`` maps each unordered name-pair to its (unique) template."""
    n = len(players)
    templates = []
    template_of_pair = {}
    for t in range(n - 1):
        h1, h2 = _pair_rr(n, t)
        games = set()
        for i in range(n // 2):
            pair = frozenset((players[h1[i]].name, players[h2[i]].name))
            games.add(pair)
            template_of_pair[pair] = t
        templates.append(games)
    return templates, template_of_pair


def _identify_template(pairset, template_of_pair) -> int:
    """The template index a played round used, from its (non-bye) game pairs.
    Every game in a round belongs to the same template, so any one pair pins it;
    we check they agree. Raises :class:`PairingError` if they don't (e.g. the
    field changed under a played round)."""
    indices = set()
    for pair in pairset:
        t = template_of_pair.get(pair)
        if t is None:
            raise PairingError(
                "A played round no longer matches the round-robin schedule "
                "(did the entrants change?)."
            )
        indices.add(t)
    if len(indices) != 1:
        raise PairingError(
            "A played round is not a valid round-robin round; cannot place fixed "
            "pairings around it."
        )
    return indices.pop()


def _rr_permutation(num_positions, template_of_pair, played, fixed):
    """Bijection ``position -> template index`` for one round-robin block, where a
    position is a round (or round-pair, for double round robin) in the block.

    A round robin is a fixed set of templates; the schedule just chooses which
    template plays at which position. Start from the identity (position ``p`` ->
    template ``p``); pin each **played** position to the template it actually used
    (a fixed point); then for each **fixed** pairing move the template containing
    that pair to the requested position by transposing it with whatever sits there.
    Locked positions (played, or already set by an earlier fixed pairing) cannot be
    disturbed — doing so raises :class:`PairingError`. Deterministic, so per-round
    callers recompute the same bijection."""
    assign = list(range(num_positions))   # position -> template
    where = list(range(num_positions))    # template -> position
    locked = set()

    def place(position, t, err):
        if assign[position] == t:
            locked.add(position)
            return
        if position in locked or where[t] in locked:
            raise PairingError(err)
        other, t_old = where[t], assign[position]
        assign[position], where[t] = t, position
        assign[other], where[t_old] = t_old, other
        locked.add(position)

    for position in sorted(played):
        place(position, played[position],
              "Played round-robin rounds conflict with the fixed pairings.")
    for position, pair in fixed:
        place(position, template_of_pair[pair],
              "Fixed pairings conflict: cannot place all of them in their "
              "requested rounds.")
    return assign


def _rr_block_pairings(pd: PairingData, rp: RoundPairing, k: int) -> Pairings:
    """Round-robin family pairing for one calendar round, honoring fixed pairings
    by permuting which template lands in which round (``k`` calendar rounds per
    template: 1 for round robin, 2 for double round robin)."""
    players = _rr_players(pd)
    num_positions = len(players) - 1
    _, template_of_pair = _rr_templates(players)

    block_rounds = {
        o.round for o in pd.round_pairings
        if o.pairing == rp.pairing and o.start_round == rp.start_round
    }
    guard_no_dropped_in_block(pd, block_rounds, "round-robin", "round robins")

    def position_of(round_number):
        return (round_number - rp.start_round) // k

    # A round robin over E players has exactly E-1 rounds (num_positions
    # templates); a double round robin has 2*(E-1). A block configured with more
    # rounds than that has no template for the overflow round — fail clearly
    # instead of indexing past the rotation.
    if position_of(rp.round) >= num_positions:
        max_rounds = num_positions * k
        raise PairingError(
            f"This round robin has only {max_rounds} "
            f"round{'s' if max_rounds != 1 else ''} for the current field; "
            f"round {rp.round} is beyond the rotation — shorten the block or "
            "add players."
        )

    # Played rounds become fixed points, identified from their recorded games.
    played_pairs = defaultdict(set)
    for s in pd.result_slips:
        if s.round in block_rounds and not (
            _is_bye_name(s.winner_name) or _is_bye_name(s.loser_name)
        ):
            played_pairs[position_of(s.round)].add(
                frozenset((s.winner_name, s.loser_name))
            )
    played = {
        position: _identify_template(pairset, template_of_pair)
        for position, pairset in played_pairs.items()
    }

    fixed = sorted(
        (
            (position_of(round_number), frozenset(pair))
            for round_number, pairs in pd.fixed_pairings.items()
            if round_number in block_rounds
            for pair in pairs
        ),
        key=lambda sp: (sp[0], sorted(sp[1])),
    )

    assign = _rr_permutation(num_positions, template_of_pair, played, fixed)

    t = assign[position_of(rp.round)]
    h1, h2 = _pair_rr(len(players), t)
    pairings = Pairings()
    for i in range(len(players) // 2):
        pairings.add(players[h1[i]], players[h2[i]])
    return pairings


def pair_round_robin(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Round robin pairing.

    See https://github.com/domino14/liwords/ for the rotation. A round robin
    rotates off a fixed ordering and never depends on results, so it seeds from
    the tournament seedings, not the start-round standings; the round's position
    (round minus the block's first round) picks the template, which fixed pairings
    may permute.
    """
    return _rr_block_pairings(pd, rp, k=1)


# -----------------------------------------------------
# Charlottesville.

# Split the field into 2 groups in a snake order.
# Group 1: 1, 4, 5, 8, 9, 12, 13, 16, 17
# Group 2: 2, 3, 6, 7, 10, 11, 14, 15, 18
# For the first 9 rounds, you play a round robin against all the people in the *other* group.

def pair_charlottesville(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Charlottesville pairing."""
    seeding = list(standings_after_round(pd, 0))
    # Odd field: add a bye so the two groups are equal in size; whoever is drawn
    # against the bye sits the round out.
    if len(seeding) % 2 != 0:
        seeding.append(Player("Bye"))
    g1 = []
    g2 = []
    for i in range(len(seeding)):
        if i % 4 == 1 or i % 4 == 3:
            g1.append(i)
        else:
            g2.append(i)
    pairings = Pairings()
    if not g2:
        return pairings
    # reverse group 2 so the top player plays the second player last
    g2.reverse()
    # rotate group 2 one place per round and pair up with group 1
    pos = (rp.round - rp.start_round) % len(g2)
    r1 = g2[:pos]
    r2 = g2[pos:]
    rotated = r2 + r1
    for i, g in enumerate(g1):
        p1 = g
        p2 = rotated[i]
        pairings.add(seeding[p1], seeding[p2])
    return pairings


# -----------------------------------------------------
# Double Round Robin

def pair_double_round_robin(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Double round robin: consecutive pairs of rounds share the same RR template,
    so each template spans two calendar rounds (k=2)."""
    return _rr_block_pairings(pd, rp, k=2)


# -----------------------------------------------------
# Random

def pair_random(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Random pairing: shuffle standings, pair consecutively."""
    standings = standings_after_round(pd, rp.start_round)
    random.shuffle(standings)
    pairings = Pairings()
    for p1, p2 in more_itertools.chunked(standings, 2):
        pairings.add(p1, p2)
    return pairings


# -----------------------------------------------------
# Random No Repeats

def pair_random_no_repeats(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Random pairing that minimizes repeat opponents via blossom matching."""
    if rp.start_round < 1:
        return pair_random(pd, rp)
    players = standings_after_round(pd, rp.start_round)
    return pair_no_repeats_blossom(players, pd.repeats)
