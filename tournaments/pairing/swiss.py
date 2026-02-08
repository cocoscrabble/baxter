from collections import deque, defaultdict
from dataclasses import dataclass
import itertools

from typing import List, Tuple

import networkx as nx
from tournaments.pairing.base import standings_after_round


class Groups:
    def __init__(self, n):
        self.groups = deque([deque([]) for _ in range(n + 1)])

    def __repr__(self):
        return repr(self.groups)

    @classmethod
    def from_standings(cls, standings):
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
    def length(self):
        return len(self.groups)

    @property
    def top(self):
        return self.groups[0]

    @property
    def bottom(self):
        return self.groups[-1]

    def compact(self):
        self.groups = deque(filter(None, self.groups))

    def balance(self):
        for curr, next in itertools.pairwise(self.groups):
            if len(curr) % 2 != 0:
                fst = next.popleft()
                curr.append(fst)

    def promote(self, i, j):
        fst = self.groups[j].popleft()
        self.groups[i].append(fst)

    def promote2(self, i):
        j = i + 1
        self.promote(i, j)
        if not self.groups[j]:
            self.promote(i, j + 1)
        else:
            self.promote(i, j)

    def merge_bottom(self):
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


def pair_swiss_initial(standings):
    pairings = []
    half = len(standings) // 2
    for i in range(half):
        pairings.append((standings[i], standings[i + half]))
    return pairings


def pair_swiss_top(groups, repeats, nrep):
    top = groups.top
    candidates = [[] for _ in range(len(top))]
    for i in range(len(top)):
        for j in range(len(top)):
            if i == j:
                continue
            reps = repeats.get(top[i].name, top[j].name)
            if reps < nrep:
                c = candidate(reps, abs(i - j), top[j].name, top[i].name)
                candidates[i].append(c)
    for c in candidates:
        c.sort()
    return candidates


def blossom(edges):
    # The nx implementation of blossom does not like negative weights.
    m = min(x[2] for x in edges) if edges else 0
    edges = [[v1, v2, w - m] for v1, v2, w in edges]
    g = nx.Graph()
    g.add_weighted_edges_from(edges)
    return list(sorted(nx.max_weight_matching(g, maxcardinality=True)))


def pair_candidates(bracket: list[list[candidate]]) -> list[tuple[str, str]]:
    edges = []
    names = {}
    inames = {}
    i = 0
    for i, player_candidates in enumerate(bracket):
        name = player_candidates[0].name2
        names[name] = i
        inames[i] = name

    for player_candidates in bracket:
        for c in player_candidates:
            # don't pair candidates too far apart
            if c.distance < 11:
                weight = -(30 * c.repeats + c.distance)
                v1 = names[c.name1]
                v2 = names[c.name2]
                edges.append([v1, v2, weight])
    b = blossom(edges)
    pairings = []
    for v1, v2 in b:
        name1 = inames[v1]
        name2 = inames[v2]
        pairings.append(pair(name1, name2, 0))
    return pairings


def pair_swiss(rp, pd):
    if rp.start_round < 1:
        seeding = standings_after_round(0, pd)
        return pair_swiss_initial(seeding)
    players = standings_after_round(rp.start_round, pd)
    names = {p.name: p for p in players}
    groups = Groups.from_standings(players)
    nrep = 1
    paired = []

    # Don't have too small a bottom group
    if groups.length > 1:
        while len(groups.bottom) < 6:
            groups.merge_bottom()
    while groups.length > 0:
        candidates = pair_swiss_top(groups, pd.repeats, nrep)
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
                # We have an unpaired candidate; increase the rep count
                nrep += 1
                if groups.length == 1:
                    break
                continue
            groups.groups.popleft()
            paired.append(pairs)
            if groups.length == 0:
                break
    out = []
    for group in paired:
        for p in group:
            out.append(p)
    out = [(names[p.name1], names[p.name2]) for p in out]
    return out
