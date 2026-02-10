import more_itertools

from tournaments.pairing.base import Pairings, RoundPairing, PairingData, standings_after_round


# -----------------------------------------------------
# King of the Hill

def pair_koth(rp: RoundPairing, pd: PairingData) -> Pairings:
    """King of the hill pairing."""
    standings = standings_after_round(rp.start_round, pd)
    pairings = Pairings()
    for p1, p2 in more_itertools.chunked(standings, 2):
        pairings.add(p1, p2)
    return pairings


# -----------------------------------------------------
# Queen of the Hill

def pair_qoth(rp: RoundPairing, pd: PairingData) -> Pairings:
    """Queen of the hill pairing."""
    standings = standings_after_round(rp.start_round, pd)
    pairings = Pairings()
    n = len(standings)
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


def pair_round_robin(rp: RoundPairing, pd: PairingData) -> Pairings:
    """Round robin pairing."""
    # See https://github.com/domino14/liwords/ for strategy

    standings = standings_after_round(rp.start_round - 1, pd)
    # Pair for game #pos in the round robin
    n = len(standings)
    pairings = Pairings()
    pos = rp.round - rp.start_round
    h1, h2 = _pair_rr(n, pos)
    for i in range(n // 2):
        pairings.add(standings[h1[i]], standings[h2[i]])
    return pairings


# -----------------------------------------------------
# Charlottesville.

# Split the field into 2 groups in a snake order.
# Group 1: 1, 4, 5, 8, 9, 12, 13, 16, 17
# Group 2: 2, 3, 6, 7, 10, 11, 14, 15, 18
# For the first 9 rounds, you play a round robin against all the people in the *other* group.

def pair_charlottesville(rp: RoundPairing, pd: PairingData) -> Pairings:
    """Charlottesville pairing."""
    seeding = standings_after_round(0, pd)
    pos = rp.round
    g1 = []
    g2 = []
    for i in range(len(pd.entrants)):
        if i % 4 == 1 or i % 4 == 3:
            g1.append(i)
        else:
            g2.append(i)
    # reverse group 2 so the top player plays the second player last
    g2.reverse()
    # rotate group 2 one place per round and pair up with group 1
    pos = (rp.round - rp.start_round) % len(g2)
    r1 = g2[:pos]
    r2 = g2[pos:]
    rotated = r2 + r1
    pairings = Pairings()
    for i, g in enumerate(g1):
        p1 = g
        p2 = rotated[i]
        pairings.add(seeding[p1], seeding[p2])
    return pairings
