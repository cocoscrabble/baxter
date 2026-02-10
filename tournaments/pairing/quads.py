from tournaments.pairing.base import Pairings, RoundPairing, PairingData, standings_after_round

# -------------------
# Quads

# We assume there are always an even number of players (one of whom might be 'bye'),
# but there might not be a divisible-by-four number. If there are 4n+2 players, we
# divide them into (n-1) quads and a final hex, and pair the hex separately in a
# group of 3 games.

# Quad pairings for four players, 0-3
Pairings4 = [
  [[0, 3], [1, 2]],
  [[0, 2], [1, 3]],
  [[0, 1], [2, 3]]
]

# Incomplete round robin for 6 players, 0-5
Pairings6 = [
  [[0, 1], [2, 3], [4, 5]],
  [[0, 2], [3, 4], [1, 5]],
  [[0, 3], [1, 4], [2, 5]]
]

def group_position_pairs(group, pos) -> list[list[int]]:
    if (len(group) == 4):
        return Pairings4[pos - 1]
    else:
        return Pairings6[pos - 1]


def pair_groups_at_position(groups, pos) -> Pairings:
    pairings = Pairings()
    for group in groups:
        p = group_position_pairs(group, pos)
        for a, b in p:
            pairings.add(group[a], group[b])
    return pairings


def get_last_quad_position(standings) -> int:
    n = len(standings)
    leftover = n % 4
    if leftover == 0:
        return n
    elif leftover == 2:
        return n - 6
    else:
        raise ValueError("uneven field for quads!")


def maybe_add_hex(quads, standings, max) -> None:
  # we have a leftover hex, add it to the quads
  n = len(standings)
  if max < n:
      quads.append(standings[max: n])


def pair_clustered_quads(pd: PairingData, rp: RoundPairing) -> Pairings:
    quads = []
    pos = rp.round - rp.start_round
    standings = standings_after_round(pd, rp.start_round)
    max = get_last_quad_position(standings)
    for i in range(0, max, 4):
        quads.append(standings[i: i + 4])
    maybe_add_hex(quads, standings, max)
    return pair_groups_at_position(quads, pos)


def pair_distributed_quads(pd: PairingData, rp: RoundPairing) -> Pairings:
    quads = []
    pos = rp.round - rp.start_round
    standings = standings_after_round(pd, rp.start_round)
    max = get_last_quad_position(standings)
    stride = max / 4
    for i in range(stride):
        quads[i] = []
    for i in range(max):
        quad = i % stride
        quads[quad].append(standings[i])
    maybe_add_hex(quads, standings, max)
    return pair_groups_at_position(quads, pos)


def pair_evans_quads(pd: PairingData, rp: RoundPairing) -> Pairings:
    # Like distributed quads but flip every other subgroup first,
    # so that the sum of opponent seeds ends up roughly equal.
    # e.g. for 12 people you would make quads from
    # 1 2 3 6 5 4 7 8 9 12 11 10
    quads = []
    pos = rp.round - rp.start_round
    standings = standings_after_round(pd, rp.start_round)
    max = get_last_quad_position(standings)
    stride = max / 4
    for i in range(stride):
        quads[i] = []

    # Generate new standings snake-style
    new_standings = []
    flip = False
    for i in range(0, max, stride):
        slice = standings[i: i + stride]
        if (flip):
            slice.reverse()
        flip = not flip
        new_standings = new_standings.extend(slice)

    # Make quads from the new standings
    for i in range(max):
        quad = i % stride
        quads[quad].append(new_standings[i])
    maybe_add_hex(quads, standings, max)
    return pair_groups_at_position(quads, pos)
