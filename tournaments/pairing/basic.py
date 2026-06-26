import random

import more_itertools

from tournaments.pairing.base import (
    Pairings,
    PairingData,
    Player,
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


def _round_robin(standings, pos) -> Pairings:
    """Run the round-robin rotation over ``standings`` for game ``pos``.

    For an odd field a synthetic bye player is appended so the rotation runs over
    an even number of slots; whoever is drawn against the bye sits the round out.
    Over a full rotation each real player byes exactly once and every pair meets
    exactly once.
    """
    standings = list(standings)
    if len(standings) % 2 != 0:
        standings.append(Player("Bye"))
    n = len(standings)
    pairings = Pairings()
    h1, h2 = _pair_rr(n, pos)
    for i in range(n // 2):
        pairings.add(standings[h1[i]], standings[h2[i]])
    return pairings


def pair_round_robin(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Round robin pairing."""
    # See https://github.com/domino14/liwords/ for strategy

    # A round robin rotates off a fixed ordering and never depends on results,
    # so it always seeds from the tournament seedings — not the standings as of
    # its start round. That lets a round-robin block anywhere in the schedule
    # pair up front (a block starting mid-event would otherwise read standings
    # for rounds not yet played and pair nobody). `pos` still uses start_round
    # (normalized to the block's first round) to pick the rotation step.
    standings = standings_after_round(pd, 0)
    pos = rp.round - rp.start_round
    return _round_robin(standings, pos)


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
    """Double round robin: consecutive pairs of rounds share the same RR pairing."""
    # Seed from the seedings, not the start-round standings (see pair_round_robin).
    standings = standings_after_round(pd, 0)
    pos = (rp.round - rp.start_round) // 2
    return _round_robin(standings, pos)


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
