from collections import deque
from dataclasses import dataclass
import itertools

from tournaments.pairing.base import (
    Pairing,
    Pairings,
    PairingData,
    Repeats,
    Standings,
    blossom,
    pair_no_repeats_blossom,
    standings_after_round,
)
from tournaments.pairing.round_pairing import RoundPairing

# The split point for SwissPlusRandom: the top SWISS_DISTANCE players are paired
# Swiss, the rest Random No Repeats.
SWISS_DISTANCE = 10

# The largest seed gap the Swiss blossom matcher will draw an edge across. Pairs
# further apart than this in the standings are never matched (equivalent to the
# Rust engine's MAX_DISTANCE = 11 with a strict `<`).
MAX_PAIRING_DISTANCE = 10

# Deterministic tie-break for the blossom matching. When several pairings are
# equally good (same repeats, same total seed-distance) there can be many
# max-weight matchings, and the Python (networkx) and Rust (rustworkx) matchers
# pick different ones. Perturbing each edge by a well-mixed per-edge value makes
# the maximum (almost surely) unique, so both engines choose the same matching.
# The primary objective is scaled up by _WEIGHT_SCALE so the perturbation can
# never override it. The per-edge value is a splitmix64 hash of the canonical
# (min, max) vertex pair — bit-for-bit identical to the Rust engine's.
_U64 = (1 << 64) - 1
_TIEBREAK_MOD = 1 << 40
_WEIGHT_SCALE = 1 << 52


def _match_tiebreak(a: int, b: int) -> int:
    x = ((a << 20) | b) & _U64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _U64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _U64
    x ^= x >> 31
    return x % _TIEBREAK_MOD


class Groups:
    def __init__(self, n):
        self.groups = deque([deque([]) for _ in range(n + 1)])

    def __repr__(self):
        return repr(self.groups)

    @classmethod
    def from_standings(cls, standings) -> "Groups":
        max_wins = max(p.wins for p in standings)
        ret = Groups(max_wins)
        for p in standings:
            ret.groups[p.wins].append(p)
        ret.compact()
        ret.groups.reverse()
        # balance groups
        ret.balance()
        ret.compact()
        return ret 

    @property
    def length(self) -> int:
        return len(self.groups)

    @property
    def top(self) -> deque:
        return self.groups[0]

    @property
    def bottom(self) -> deque:
        return self.groups[-1]

    def compact(self) -> None:
        self.groups = deque(filter(None, self.groups))

    def balance(self) -> None:
        for curr, next in itertools.pairwise(self.groups):
            if len(curr) % 2 != 0:
                fst = next.popleft()
                curr.append(fst)

    def promote(self, i, j) -> None:
        fst = self.groups[j].popleft()
        self.groups[i].append(fst)

    def promote2(self, i) -> None:
        j = i + 1
        self.promote(i, j)
        if not self.groups[j]:
            self.promote(i, j + 1)
        else:
            self.promote(i, j)

    def merge_bottom(self) -> None:
        if len(self.groups) == 1:
            # only one group, bailing out!
            return
        last = self.groups.pop()
        self.groups[-1] += last


@dataclass(order=True)
class candidate:
    repeats: int
    distance: int
    name1: str
    name2: str


@dataclass
class pair:
    name1: str
    name2: str
    repeats: int


def pair_swiss_initial(standings) -> Pairings:
    pairings = Pairings()
    half = len(standings) // 2
    for i in range(half):
        pairings.add(standings[i], standings[i + half])
    return pairings


def pair_swiss_top(groups, repeats, nrep) -> list[list[candidate]]:
    top = groups.top
    candidates = [[] for _ in range(len(top))]
    for i in range(len(top)):
        for j in range(len(top)):
            if i == j:
                continue
            reps = repeats.get(Pairing(top[i], top[j]))
            if reps < nrep:
                c = candidate(reps, abs(i - j), top[j].name, top[i].name)
                candidates[i].append(c)
    for c in candidates:
        c.sort()
    return candidates


def pair_candidates(bracket: list[list[candidate]]) -> list[pair]:
    edges = []
    names = {}
    inames = {}
    for i, player_candidates in enumerate(bracket):
        name = player_candidates[0].name2
        names[name] = i
        inames[i] = name

    for player_candidates in bracket:
        for c in player_candidates:
            # don't pair candidates too far apart
            if c.distance <= MAX_PAIRING_DISTANCE:
                v1 = names[c.name1]
                v2 = names[c.name2]
                a, b = (v1, v2) if v1 <= v2 else (v2, v1)
                weight = (
                    _WEIGHT_SCALE * -(30 * c.repeats + c.distance)
                    + _match_tiebreak(a, b)
                )
                edges.append([v1, v2, weight])
    b = blossom(edges)
    pairings = []
    for v1, v2 in b:
        name1 = inames[v1]
        name2 = inames[v2]
        pairings.append(pair(name1, name2, 0))
    return pairings


def _pair_swiss_players(players: Standings, repeats: Repeats) -> Pairings:
    """Core Swiss pairing logic for a list of players."""
    names = {p.name: p for p in players}
    groups = Groups.from_standings(players)
    nrep = 1
    # Termination guard: a pair can have met at most as many times as rounds
    # played, which is bounded by the field size. Once nrep exceeds this, raising
    # it further can't unlock any more edges, so a still-incomplete group is
    # genuinely unpairable (e.g. blocked by the distance cap) and we give up.
    max_nrep = len(players) + 1
    paired = []

    # Don't have too small a bottom group
    if groups.length > 1:
        while len(groups.bottom) < 6:
            groups.merge_bottom()
            if groups.length <= 1:
                # Merging has collapsed everything into a single group that is
                # still under 6; merge_bottom is now a no-op, so stop to avoid
                # spinning forever.
                break
    while groups.length > 0:
        if nrep > max_nrep:
            break
        candidates = pair_swiss_top(groups, repeats, nrep)
        if any(len(x) == 0 for x in candidates):
            if groups.length == 1:
                nrep += 1
                continue
            groups.compact()
            groups.promote2(0)
            groups.compact()
            if groups.length == 1:
                nrep += 1
                continue
        else:
            pairs = pair_candidates(candidates)
            groups.compact()
            if not pairs or len(pairs) != len(candidates) // 2:
                # Couldn't fully pair the top group at this repeat limit. Allow
                # one more repeat and retry — including when it's the last group
                # left. (Previously the last group was abandoned here, dropping
                # those players and producing a short round.)
                nrep += 1
                continue
            groups.groups.popleft()
            paired.append(pairs)
            if groups.length == 0:
                break
    out = Pairings()
    for group in paired:
        for p in group:
            out.add(names[p.name1], names[p.name2])
    return out


def pair_swiss(pd: PairingData, rp: RoundPairing) -> Pairings:
    if rp.start_round < 1:
        seeding = standings_after_round(pd, 0)
        return pair_swiss_initial(seeding)
    players = standings_after_round(pd, rp.start_round)
    return _pair_swiss_players(players, pd.repeats)


def pair_swiss_plus_random(pd: PairingData, rp: RoundPairing) -> Pairings:
    """Top SWISS_DISTANCE players paired Swiss, rest paired Random No Repeats."""
    if rp.start_round < 1:
        seeding = standings_after_round(pd, 0)
        return pair_swiss_initial(seeding)
    players = standings_after_round(pd, rp.start_round)
    swiss_players = players[:SWISS_DISTANCE]
    rand_players = players[SWISS_DISTANCE:]
    swiss_pairings = _pair_swiss_players(swiss_players, pd.repeats)
    random_pairings = pair_no_repeats_blossom(rand_players, pd.repeats)
    out = Pairings()
    out.pairings = swiss_pairings.pairings + random_pairings.pairings
    return out
