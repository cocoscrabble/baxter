//! Upstream's `copMinWeightMatching` and its policies (`pkg/pair/cop/cop.go`):
//! who may play whom (constraint policies), what each pairing costs (weight
//! policies), the min-weight matching, the one retry, and the final pairings.
//!
//! Player ids are `i64` with upstream's sentinels: `-1` for "none", and
//! `BYE_PLAYER_INDEX` (0xFFFF) for the bye node. Nodes are indexed by rank; the
//! bye, when there is one, is the last node.

use std::collections::HashMap;

use serde::Serialize;

use super::precomp::{
    compute_top_down_bye_rank_idx, extract_prepaired_players, is_last_quarter, pairing_key,
    PrecompData,
};
use super::request::Request;
use super::standings::BYE_PLAYER_INDEX;
use super::verify::PairError;
use crate::matching::max_weight_matching_pairs;

const MAJOR_PENALTY: i64 = 1_000_000_000;
const MINOR_PENALTY: i64 = MAJOR_PENALTY / 1000;
/// Giving a hopeful-to-cash contender the bye; above two major penalties, so
/// two other violations are preferred to it.
const HOPEFUL_CASHER_BYE_WEIGHT: i64 = 3_000_000_000;
const CONTROL_LOSS_LOWEST_CONTENDER_ONLY_ROUNDS: i32 = 4;
const BYE: i64 = BYE_PLAYER_INDEX as i64;

/// The weight policies, in upstream's order (the log's column order).
pub const WEIGHT_POLICIES: [&str; 7] = ["RD", "PC", "CC", "GC", "RE", "BB", "BR"];

struct Pargs<'a> {
    req: &'a Request,
    pd: &'a PrecompData,
    nodes: Vec<i64>,
    lowest_possible_abs_casher: i64,
    lowest_possible_hope_casher: i64,
    rounds_remaining: i32,
    round_pairings_remaining: i32,
    gibson_gets_bye: bool,
    prepaired_round_idx: i32,
    prepaired: HashMap<usize, usize>,
    lowest_hope_override: HashMap<usize, i64>,
    factor3_forced: Vec<(i64, i64)>,
    top_down_bye_player: i64,
    disallowed_leader_opponent: i64,
    forced_leader_vs_third: i64,
    forced_contender_bye_player: i64,
    top4_lock_active: bool,
}

impl Pargs<'_> {
    /// Nodes excluding a trailing bye.
    fn num_real_nodes(&self) -> usize {
        let n = self.nodes.len();
        if n > 0 && self.nodes[n - 1] == BYE {
            n - 1
        } else {
            n
        }
    }

    fn gibsonized(&self, rank: usize) -> bool {
        self.pd.gibsonized_players.get(rank).copied().unwrap_or(false)
    }

    /// The definite bye recipient, if the top-down or forced-contender bye
    /// picked one.
    fn bye_recipient(&self) -> i64 {
        if self.top_down_bye_player >= 0 {
            self.top_down_bye_player
        } else if self.forced_contender_bye_player >= 0 {
            self.forced_contender_bye_player
        } else {
            -1
        }
    }

    fn times_played(&self, a: i64, b: i64) -> usize {
        self.pd.pairing_counts.get(&pairing_key(a as usize, b as usize)).copied().unwrap_or(0)
    }

    fn is_last_quarter(&self) -> bool {
        is_last_quarter(self.round_pairings_remaining, self.req.rounds)
    }

    fn is_final_round(&self) -> bool {
        self.rounds_remaining == 1
    }
}

// ---------------------------------------------------------------------------
// Pre-matching decisions (computed once, in upstream's order)
// ---------------------------------------------------------------------------

/// `computeTopDownByePlayer`.
fn compute_top_down_bye_player(p: &Pargs) -> i64 {
    let rank = compute_top_down_bye_rank_idx(p.req, &p.pd.standings, &p.pd.pairing_counts);
    if rank < 0 {
        -1
    } else {
        p.pd.standings.player_index(rank as usize) as i64
    }
}

/// `computeForcedContenderBye`: when "no bye for contenders" (PC) and "no
/// repeat byes" (BR) conflict, force the bye onto the lowest-ranked contender
/// with the fewest byes.
fn compute_forced_contender_bye(p: &Pargs) -> i64 {
    let n = p.nodes.len();
    if p.req.allow_repeat_byes || p.nodes[n - 1] != BYE || p.gibson_gets_bye || p.top_down_bye_player >= 0 {
        return -1;
    }
    let mut bye_player = -1;
    let mut least = p.req.rounds as usize + 1;
    for rank in (0..n - 1).rev() {
        let pi = p.nodes[rank];
        let byes = p.times_played(pi, BYE);
        let contender = rank as i64 <= p.lowest_possible_hope_casher && !p.gibsonized(rank);
        if byes == 0 && !contender {
            return -1;
        }
        if contender && byes < least {
            least = byes;
            bye_player = pi;
        }
    }
    if bye_player < 0 || least > 0 {
        return -1;
    }
    bye_player
}

/// `computeForcedLeaderVsThird`: rank 2 on the bye while at most three are
/// hopeful for 1st forces the leader onto 3rd.
fn compute_forced_leader_vs_third(p: &Pargs) -> i64 {
    if !p.factor3_forced.is_empty() {
        return -1;
    }
    if p.pd.lowest_possible_hope_nth[0] > 2 || p.gibsonized(0) || p.num_real_nodes() < 3 {
        return -1;
    }
    let bye = p.bye_recipient();
    if bye < 0 || p.nodes[1] != bye {
        return -1;
    }
    p.nodes[2]
}

/// `computeDisallowedLeaderOpponent`: an odd hopeful-for-1st group pulls in the
/// next player down, who is then barred from playing the leader.
fn compute_disallowed_leader_opponent(p: &Pargs) -> i64 {
    if !p.factor3_forced.is_empty() || p.forced_leader_vs_third >= 0 {
        return -1;
    }
    let n = p.num_real_nodes();
    if n < 1 {
        return -1;
    }
    let boundary = p.pd.lowest_possible_hope_nth[0] as i64;
    let raw_group = boundary + 1;
    let extra = (boundary + 1) as usize;
    // A lone contender (the leader) is handled on its own: only a gibsonized
    // leader pulls the next player in, to protect a genuine 2nd/3rd race.
    if raw_group == 1 {
        if !p.gibsonized(0) || extra >= n {
            return -1;
        }
        return p.nodes[extra];
    }
    let mut group = raw_group;
    if p.gibsonized(0) {
        group -= 1;
    }
    let bye = p.bye_recipient();
    if bye >= 0 && (0..=boundary as usize).any(|r| p.nodes[r] == bye) {
        group -= 1;
    }
    if group % 2 == 0 || extra >= n {
        return -1;
    }
    p.nodes[extra]
}

/// `computeTop4LockActive`: exactly four hopeful for 1st with two rounds left
/// (and no Factor 3) keeps those four playing each other.
fn compute_top4_lock_active(p: &Pargs) -> bool {
    if !p.factor3_forced.is_empty() || p.rounds_remaining != 2 || p.pd.lowest_possible_hope_nth[0] != 3 {
        return false;
    }
    if p.num_real_nodes() < 4 {
        return false;
    }
    let bye = p.bye_recipient();
    !(bye >= 0 && (0..=3).any(|r| p.nodes[r] == bye))
}

/// `adjustLowestPossibleHopeCasherForBye`: once a bye recipient inside the
/// hopeful-to-cash group is known, restore the group's parity.
fn adjust_lowest_possible_hope_casher_for_bye(p: &Pargs, num_players: usize) -> i64 {
    let bye = p.bye_recipient();
    if bye < 0 {
        return p.lowest_possible_hope_casher;
    }
    let bye_rank = match (0..num_players).find(|&r| p.nodes[r] == bye) {
        Some(r) => r as i64,
        None => return p.lowest_possible_hope_casher,
    };
    if bye_rank > p.lowest_possible_hope_casher || p.gibsonized(bye_rank as usize) {
        return p.lowest_possible_hope_casher;
    }
    let promoted = p.pd.hopeful_to_cash_promoted_rank_idx as i64;
    if promoted >= 0 && bye_rank < promoted {
        // The bye landed on a genuine contender: the earlier promotion is
        // redundant, so retract it rather than extend again.
        return promoted - 1;
    }
    let extended = p.lowest_possible_hope_casher + 1;
    if extended >= num_players as i64 {
        return p.lowest_possible_hope_casher;
    }
    extended
}

/// `hopeToCashBoundary`: in fields of 12+, the bottom six never count as
/// hopeful to cash.
fn hope_to_cash_boundary(p: &Pargs) -> i64 {
    let boundary = p.lowest_possible_hope_casher;
    let n = p.num_real_nodes();
    if n < 12 {
        return boundary;
    }
    boundary.min(n as i64 - 7)
}

// ---------------------------------------------------------------------------
// Constraint policies: (forced pairs, disallowed pairs), by player id
// ---------------------------------------------------------------------------

/// Pairs of player ids.
type Pairs = Vec<(i64, i64)>;
/// A constraint policy's verdict: (forced pairs, disallowed pairs).
type Verdict = (Pairs, Pairs);

type Constraint = (&'static str, fn(&Pargs) -> Verdict);

const CONSTRAINT_POLICIES: [Constraint; 10] = [
    ("PP", pp),
    ("KH", kh),
    ("CL", cl),
    ("GG", gg),
    ("GB", gb),
    ("TB", tb),
    ("CB", cb),
    ("F3", f3),
    ("T4", t4),
    ("L3", l3),
];

/// Prepaired players pair with nobody here; their games are placed as given.
fn pp(p: &Pargs) -> Verdict {
    if p.prepaired_round_idx == -1 {
        return (vec![], vec![]);
    }
    let mut disallowed = Vec::new();
    let mut players: Vec<usize> = p.prepaired.keys().copied().collect();
    players.sort_unstable(); // map order is irrelevant upstream; keep ours stable
    for player in players {
        for &node in &p.nodes {
            disallowed.push((player as i64, node));
        }
    }
    (vec![], disallowed)
}

/// King of the hill in the final round, for cash then for class prizes.
fn kh(p: &Pargs) -> Verdict {
    if p.rounds_remaining != 1 {
        return (vec![], vec![]);
    }
    let mut forced = Vec::new();
    // The top-down bye recipient has no opponent at all; drop them from the
    // scan so their neighbour is not skipped too.
    let ranks: Vec<usize> =
        (0..p.nodes.len()).filter(|&r| !(p.top_down_bye_player >= 0 && p.nodes[r] == p.top_down_bye_player)).collect();
    let mut highest_noncontender = 0usize;
    let mut k = 0usize;
    while k + 1 < ranks.len() {
        let rank = ranks[k];
        if p.lowest_possible_abs_casher < rank as i64 {
            highest_noncontender = rank;
            break;
        }
        let next = ranks[k + 1];
        let (pi, pj) = (p.nodes[rank], p.nodes[next]);
        if pi == BYE || pj == BYE || p.gibsonized(rank) || p.gibsonized(next) {
            k += 1;
            continue;
        }
        if rank == 0 && (pi == p.disallowed_leader_opponent || pj == p.disallowed_leader_opponent) {
            k += 1;
            continue;
        }
        forced.push((pi, pj));
        k += 2;
    }

    // Class prize KOTH.
    let num_players = p.num_real_nodes();
    let class_of = |node: i64| p.req.player_classes[node as usize];
    let koth_cume = p.req.gibson_spread as i64 * 2;
    let standings = &p.pd.standings;
    for (idx, &prizes) in p.req.class_prizes.iter().enumerate() {
        let class = idx as i32 + 1;
        let mut available = prizes;
        for rank in 0..highest_noncontender {
            let player = standings.player_index(rank);
            if p.req.player_classes[player] == class
                && p.pd.lowest_rank_absolutely[rank] >= p.req.place_prizes as usize
            {
                available -= 1;
            }
        }
        if available < 1 {
            continue;
        }
        let mut ri = highest_noncontender;
        let mut ahead = 0;
        let mut to_catch: i64 = -1;
        let skip = |r: usize| class_of(p.nodes[r]) != class || p.nodes[r] == p.top_down_bye_player;
        loop {
            while ri < num_players && skip(ri) {
                ri += 1;
            }
            let mut rj = ri + 1;
            while rj < num_players && skip(rj) {
                rj += 1;
            }
            if rj >= num_players {
                break;
            }
            if !standings.can_catch(1, koth_cume, ri, rj) {
                ri = rj;
                ahead += 1;
                if ahead == available {
                    break;
                }
                continue;
            }
            let places_remaining = available - ahead;
            if places_remaining == 2 {
                to_catch = rj as i64;
            } else if places_remaining == 1 {
                to_catch = ri as i64;
            } else if to_catch >= 0 && !standings.can_catch(1, koth_cume, to_catch as usize, rj) {
                break;
            }
            forced.push((p.nodes[ri], p.nodes[rj]));
            ahead += 2;
            ri = rj + 1;
        }
    }
    (forced, vec![])
}

/// Control loss: the leader plays the destiny's child (or, earlier on, the
/// child or the rank above).
fn cl(p: &Pargs) -> Verdict {
    let child = p.pd.destinys_child;
    if child < 0 || !p.factor3_forced.is_empty() {
        return (vec![], vec![]);
    }
    let mut disallowed = Vec::new();
    for rank in 1..p.nodes.len() as i32 {
        if rank == child
            || (p.rounds_remaining > CONTROL_LOSS_LOWEST_CONTENDER_ONLY_ROUNDS
                && p.req.control_loss_activation_round != p.pd.complete_pairings as i32
                && rank == child - 1)
        {
            continue;
        }
        disallowed.push((p.nodes[0], p.nodes[rank as usize]));
    }
    (vec![], disallowed)
}

/// Gibson groups play within themselves.
fn gg(p: &Pargs) -> Verdict {
    let n = p.num_real_nodes();
    let mut disallowed = Vec::new();
    for i in 0..n {
        for j in i + 1..n {
            if p.pd.gibson_groups[i] != p.pd.gibson_groups[j] {
                disallowed.push((p.nodes[i], p.nodes[j]));
            }
        }
    }
    (vec![], disallowed)
}

/// The bye goes to a gibsonized player.
fn gb(p: &Pargs) -> Verdict {
    if !p.gibson_gets_bye || p.top_down_bye_player >= 0 {
        return (vec![], vec![]);
    }
    let disallowed = (0..p.num_real_nodes())
        .filter(|&r| !p.gibsonized(r))
        .map(|r| (p.nodes[r], BYE))
        .collect();
    (vec![], disallowed)
}

fn tb(p: &Pargs) -> Verdict {
    if p.top_down_bye_player < 0 {
        return (vec![], vec![]);
    }
    (vec![(p.top_down_bye_player, BYE)], vec![])
}

fn cb(p: &Pargs) -> Verdict {
    if p.forced_contender_bye_player < 0 {
        return (vec![], vec![]);
    }
    (vec![(p.forced_contender_bye_player, BYE)], vec![])
}

fn f3(p: &Pargs) -> Verdict {
    (p.factor3_forced.clone(), vec![])
}

/// Top-4 lock: the four hopefuls for 1st may play nobody outside the four.
fn t4(p: &Pargs) -> Verdict {
    if !p.top4_lock_active {
        return (vec![], vec![]);
    }
    let mut disallowed = Vec::new();
    for i in 0..=3 {
        for j in 4..p.nodes.len() {
            disallowed.push((p.nodes[i], p.nodes[j]));
        }
    }
    (vec![], disallowed)
}

fn l3(p: &Pargs) -> Verdict {
    if p.forced_leader_vs_third < 0 {
        return (vec![], vec![]);
    }
    (vec![(p.nodes[0], p.forced_leader_vs_third)], vec![])
}

// ---------------------------------------------------------------------------
// Weight policies: the cost of ranks `ri < rj` playing each other
// ---------------------------------------------------------------------------

/// Rank difference: cubed, or squared when gibsonization is involved or the
/// higher player is out of the money. Zero for cashers in the fourth quarter,
/// where PC weighs them instead.
fn rd(p: &Pargs, ri: usize, rj: usize) -> i64 {
    let diff = (rj - ri) as i64;
    if p.is_last_quarter()
        && !p.is_final_round()
        && !p.gibsonized(ri)
        && ri as i64 <= p.lowest_possible_hope_casher
    {
        return 0;
    }
    if p.gibsonized(ri) || p.gibsonized(rj) || ri >= p.req.place_prizes as usize {
        diff * diff
    } else {
        diff * diff * diff
    }
}

/// Pair with casher: contenders play within their contention window.
fn pc(p: &Pargs, ri: usize, rj: usize) -> i64 {
    if p.is_final_round() {
        return 0;
    }
    if p.gibsonized(ri) || p.gibsonized(rj) || ri as i64 > p.lowest_possible_hope_casher {
        return 0;
    }
    if p.nodes[rj] == BYE {
        // TB already forces this edge; penalizing it would only trigger a retry.
        if p.nodes[ri] == p.top_down_bye_player {
            return 0;
        }
        return HOPEFUL_CASHER_BYE_WEIGHT;
    }
    let lowest = p
        .lowest_hope_override
        .get(&ri)
        .copied()
        .unwrap_or(p.pd.lowest_possible_hope_nth[ri] as i64);
    let rj_i = rj as i64;
    if rj_i <= lowest || (lowest == ri as i64 && ri == rj - 1) {
        if !p.is_last_quarter() {
            return 0;
        }
        let diff = (lowest - rj_i).abs() as f64;
        return (diff.powi(3) * 2.0) as i64;
    }
    // Just outside the window costs half, so a forced reach goes one rank.
    if rj_i == lowest + 1 {
        return MAJOR_PENALTY / 2;
    }
    MAJOR_PENALTY
}

/// Cash contention: hopeful plays hopeful, hopeful avoids the bottom six, the
/// odd group's extra player avoids the leader — as penalties, so they cannot
/// stack into an unsatisfiable constraint.
fn cc(p: &Pargs, ri: usize, rj: usize) -> i64 {
    if ri == 0 && p.forced_leader_vs_third >= 0 && p.nodes[rj] == p.forced_leader_vs_third {
        return 0;
    }
    if ri == 0 && p.disallowed_leader_opponent >= 0 && p.nodes[rj] == p.disallowed_leader_opponent {
        return MAJOR_PENALTY;
    }
    if !p.is_last_quarter() || p.is_final_round() || p.nodes[rj] == BYE {
        return 0;
    }
    let boundary = hope_to_cash_boundary(p);
    let ri_hopeful = ri as i64 <= boundary && !p.gibsonized(ri);
    let rj_hopeful = rj as i64 <= boundary && !p.gibsonized(rj);
    if ri_hopeful != rj_hopeful {
        if rj as i64 == boundary + 1 {
            return MAJOR_PENALTY / 2;
        }
        return MAJOR_PENALTY;
    }
    let n = p.num_real_nodes();
    if n >= 12 {
        let bottom = n - 6;
        let (ri_bottom, rj_bottom) = (ri >= bottom, rj >= bottom);
        if (ri_hopeful && rj_bottom) || (rj_hopeful && ri_bottom) {
            if ri == bottom || rj == bottom {
                return MAJOR_PENALTY / 2;
            }
            return MAJOR_PENALTY;
        }
    }
    0
}

/// Gibson cashers: a gibsonized player avoids anyone who can still cash.
fn gc(p: &Pargs, ri: usize, rj: usize) -> i64 {
    let rj_group = p.pd.gibson_groups.get(rj).copied().unwrap_or(0);
    let (gi, gj) = (p.gibsonized(ri), p.gibsonized(rj));
    if p.pd.gibson_groups[ri] != 0 || rj_group != 0 || (gi && gj) {
        return 0;
    }
    if (gi && rj as i64 <= p.lowest_possible_abs_casher) || (gj && ri as i64 <= p.lowest_possible_abs_casher) {
        return MAJOR_PENALTY;
    }
    0
}

/// Repeats: RE(n) = 2 RE(n-1) + 1 units, plus a tenth of a unit for 1st v 2nd.
fn re(p: &Pargs, ri: usize, rj: usize) -> i64 {
    let times = p.times_played(p.nodes[ri], p.nodes[rj]);
    let n = p.pd.standings.num_players() as f64;
    let unit = 4 * ((n / 3.0).powi(3) as i64);
    let multiplier = if times > 0 { (1i64 << times) - 1 } else { 0 };
    let mut weight = multiplier * unit;
    if ri == 0 && rj == 1 {
        weight += unit / 10;
    }
    weight
}

/// Back-to-back repeats outside the hopeful-to-cash group.
fn bb(p: &Pargs, ri: usize, rj: usize) -> i64 {
    if ri as i64 <= p.lowest_possible_hope_casher {
        return 0;
    }
    let mut last = p.pd.complete_pairings as i32 - 1;
    if p.prepaired_round_idx >= 0 {
        last = p.prepaired_round_idx - 1;
    }
    if last < 0 {
        return 0;
    }
    let (pi, pj) = (p.nodes[ri], p.nodes[rj]);
    if p.req.division_pairings[last as usize][pi as usize] as i64 != pj {
        return 0;
    }
    MINOR_PENALTY
}

/// Bye repeats.
fn br(p: &Pargs, ri: usize, rj: usize) -> i64 {
    if p.req.allow_repeat_byes || p.nodes[rj] != BYE {
        return 0;
    }
    MAJOR_PENALTY * p.times_played(p.nodes[ri], BYE) as i64
}

const WEIGHT_FNS: [fn(&Pargs, usize, usize) -> i64; 7] = [rd, pc, cc, gc, re, bb, br];

// ---------------------------------------------------------------------------
// Matching
// ---------------------------------------------------------------------------

/// One row of the log's "Pairing Weights" table: ranks `i < j` (the bye node is
/// the last index), and either the constraint that barred the pair or its
/// weights.
#[derive(Debug, Clone, Serialize)]
pub struct WeightRow {
    pub i: usize,
    pub j: usize,
    pub code: Option<&'static str>,
    pub times_played: usize,
    pub total: i64,
    pub weights: Vec<i64>,
    pub selected: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct MatchingTrace {
    /// Player id at each node (the bye is 65535).
    pub nodes: Vec<i64>,
    pub weights: Vec<WeightRow>,
    pub total_weight: i64,
    pub retried: bool,
    pub destinys_child: Option<usize>,
    pub lowest_hope_casher: i64,
    pub lowest_abs_casher: i64,
    pub top_down_bye_player: i64,
    pub forced_contender_bye_player: i64,
    pub forced_leader_vs_third: i64,
    pub disallowed_leader_opponent: i64,
    pub top4_lock_active: bool,
    pub gibson_gets_bye: bool,
}

/// `matching.MinWeightMatching(edges, true)`: min-weight max-cardinality
/// matching as a mate array sized to the highest vertex in any edge, and the
/// total of the chosen edges' (original) weights.
fn min_weight_matching(edges: &[(usize, usize, i64)]) -> (Vec<i64>, i64) {
    let nvertex = edges.iter().map(|&(i, j, _)| i.max(j) + 1).max().unwrap_or(0);
    let max_w = edges.iter().map(|&(_, _, w)| w).max().unwrap_or(-1).max(-1);
    let inverted: Vec<(usize, usize, i128)> =
        edges.iter().map(|&(i, j, w)| (i, j, (max_w - w) as i128)).collect();
    let mut mate = vec![-1i64; nvertex];
    let mut total = 0;
    let weight_of: HashMap<(usize, usize), i64> =
        edges.iter().map(|&(i, j, w)| ((i.min(j), i.max(j)), w)).collect();
    for (a, b) in max_weight_matching_pairs(nvertex, &inverted) {
        mate[a] = b as i64;
        mate[b] = a as i64;
        total += weight_of[&(a.min(b), a.max(b))];
    }
    (mate, total)
}

/// `copMinWeightMatching`: returns `allPlayerPairings` (player index → opponent
/// index, a bye as the player's own index, -1 for removed players).
pub fn cop_min_weight_matching(
    req: &Request,
    pd: &PrecompData,
    factor3_forced: &[(usize, usize)],
) -> (Result<Vec<i32>, PairError>, MatchingTrace) {
    let (prepaired, forced_byes, prepaired_round_idx) = extract_prepaired_players(req);
    let standings = &pd.standings;
    let num_players = standings.num_players();
    let mut nodes: Vec<i64> = (0..num_players).map(|r| standings.player_index(r) as i64).collect();
    let add_bye = (num_players - forced_byes) % 2 == 1;
    if add_bye {
        nodes.push(BYE);
    }

    let place_prizes = req.place_prizes as usize;
    let lowest_possible_abs_casher = pd
        .highest_rank_absolutely
        .iter()
        .rposition(|&place| place < place_prizes)
        .map_or(0, |rank| rank as i64);
    let lowest_possible_hope_casher = pd.lowest_possible_hope_nth[place_prizes - 1] as i64;

    // Upstream stops at the bye node too, which never sits among the ranks.
    let gibson_gets_bye = add_bye
        && (0..num_players)
            .take_while(|&i| nodes[i] != BYE)
            .any(|i| pd.gibsonized_players[i] && pd.gibson_groups[i] == 0);

    let mut p = Pargs {
        req,
        pd,
        nodes,
        lowest_possible_abs_casher,
        lowest_possible_hope_casher,
        rounds_remaining: req.rounds_remaining(),
        round_pairings_remaining: req.rounds - pd.complete_pairings as i32,
        gibson_gets_bye,
        prepaired_round_idx,
        prepaired,
        lowest_hope_override: HashMap::new(),
        factor3_forced: factor3_forced.iter().map(|&(a, b)| (a as i64, b as i64)).collect(),
        top_down_bye_player: -1,
        disallowed_leader_opponent: -1,
        forced_leader_vs_third: -1,
        forced_contender_bye_player: -1,
        top4_lock_active: false,
    };
    p.top_down_bye_player = compute_top_down_bye_player(&p);
    p.forced_contender_bye_player = compute_forced_contender_bye(&p);
    p.forced_leader_vs_third = compute_forced_leader_vs_third(&p);
    p.disallowed_leader_opponent = compute_disallowed_leader_opponent(&p);
    p.top4_lock_active = compute_top4_lock_active(&p);
    p.lowest_possible_hope_casher = adjust_lowest_possible_hope_casher_for_bye(&p, num_players);

    // Disallowed pairs, by pairing key: the first policy to bar a pair names it.
    let mut disallowed: HashMap<(usize, usize), &'static str> = HashMap::new();
    let key = |a: i64, b: i64| pairing_key(a as usize, b as usize);
    for (name, policy) in CONSTRAINT_POLICIES {
        let (forced, barred) = policy(&p);
        for (a, b) in barred {
            disallowed.entry(key(a, b)).or_insert(name);
        }
        for (fa, fb) in forced {
            for i in 0..p.nodes.len() {
                for j in i + 1..p.nodes.len() {
                    let (pi, pj) = (p.nodes[i], p.nodes[j]);
                    if (fa == pi && fb == pj) || (fb == pi && fa == pj) {
                        continue;
                    }
                    if fa == pi || fb == pi || fa == pj || fb == pj {
                        disallowed.entry(key(pi, pj)).or_insert(name);
                    }
                }
            }
        }
    }

    let n_nodes = p.nodes.len();
    let mut retried = false;
    let (mut mate, mut total_weight, mut rows);
    loop {
        let mut edges = Vec::new();
        let mut pc_weights: HashMap<(usize, usize), i64> = HashMap::new();
        rows = Vec::new();
        for i in 0..n_nodes {
            for j in i + 1..n_nodes {
                let times = p.times_played(p.nodes[i], p.nodes[j]);
                if let Some(&code) = disallowed.get(&key(p.nodes[i], p.nodes[j])) {
                    rows.push(WeightRow {
                        i, j, code: Some(code), times_played: times, total: 0,
                        weights: Vec::new(), selected: false,
                    });
                    continue;
                }
                let weights: Vec<i64> = WEIGHT_FNS.iter().map(|f| f(&p, i, j)).collect();
                let total: i64 = weights.iter().sum();
                pc_weights.insert((i, j), weights[1]);
                edges.push((i, j, total));
                rows.push(WeightRow {
                    i, j, code: None, times_played: times, total, weights, selected: false,
                });
            }
        }

        let (m, t) = min_weight_matching(&edges);
        mate = m;
        total_weight = t;
        if add_bye {
            mate.pop();
        }
        match mate.len().cmp(&num_players) {
            std::cmp::Ordering::Greater => {
                let trace = trace_of(&p, rows, total_weight, retried);
                return (
                    Err(PairError {
                        code: "INVALID_PAIRINGS_LENGTH",
                        message: format!("invalid pairings length {} for {num_players} players", mate.len()),
                    }),
                    trace,
                );
            }
            std::cmp::Ordering::Less => mate.resize(num_players, -1),
            std::cmp::Ordering::Equal => {}
        }

        // A chosen edge with a major PC penalty widens that player's contention
        // window by a rank; retry once.
        if retried {
            break;
        }
        let mut expanded = false;
        let mut overrides: HashMap<usize, i64> = HashMap::new();
        for (r, &opp) in mate.iter().enumerate() {
            if opp <= r as i64 {
                continue;
            }
            if pc_weights.get(&(r, opp as usize)).copied().unwrap_or(0) >= MAJOR_PENALTY
                && r < pd.lowest_possible_hope_nth.len()
            {
                let current = overrides.get(&r).copied().unwrap_or(pd.lowest_possible_hope_nth[r] as i64);
                if current + 1 < num_players as i64 {
                    overrides.insert(r, current + 1);
                    expanded = true;
                }
            }
        }
        if !expanded {
            break;
        }
        p.lowest_hope_override = overrides;
        retried = true;
    }

    for (r, &opp) in mate.iter().enumerate() {
        if opp < r as i64 {
            continue;
        }
        if let Some(row) = rows.iter_mut().find(|row| row.i == r && row.j == opp as usize) {
            row.selected = true;
        }
    }
    let trace = trace_of(&p, rows, total_weight, retried);

    let mut all = vec![-1i32; req.all_players as usize];
    let mut unpaired = Vec::new();
    for (r, &opp) in mate.iter().enumerate() {
        let player = p.nodes[r] as usize;
        let prepaired_opp = p.prepaired.get(&player).copied();
        if opp < 0 {
            match prepaired_opp {
                Some(o) => all[player] = o as i32,
                None => unpaired.push(player),
            }
        } else if prepaired_opp.is_some() {
            return (
                Err(PairError {
                    code: "OVERCONSTRAINED",
                    message: format!("player {} is prepaired but was still paired by COP", req.player_names[player]),
                }),
                trace,
            );
        } else {
            let mut o = p.nodes[opp as usize];
            if o == BYE {
                o = player as i64;
            }
            all[player] = o as i32;
        }
    }
    for &removed in &req.removed_players {
        if all[removed as usize] != -1 {
            return (
                Err(PairError {
                    code: "OVERCONSTRAINED",
                    message: format!(
                        "player {} was removed but was still paired by COP",
                        req.player_names[removed as usize]
                    ),
                }),
                trace,
            );
        }
    }
    if !unpaired.is_empty() {
        let mut msg = String::from(
            "COP pairings could not be completed because there were too many constraints. The unpaired players are:\n\n",
        );
        for u in unpaired {
            msg.push_str(&req.player_names[u]);
            msg.push('\n');
        }
        return (Err(PairError { code: "OVERCONSTRAINED", message: msg }), trace);
    }
    (Ok(all), trace)
}

fn trace_of(p: &Pargs, weights: Vec<WeightRow>, total_weight: i64, retried: bool) -> MatchingTrace {
    MatchingTrace {
        nodes: p.nodes.clone(),
        weights,
        total_weight,
        retried,
        destinys_child: (p.pd.destinys_child >= 0)
            .then(|| p.pd.standings.player_index(p.pd.destinys_child as usize)),
        lowest_hope_casher: p.lowest_possible_hope_casher,
        lowest_abs_casher: p.lowest_possible_abs_casher,
        top_down_bye_player: p.top_down_bye_player,
        forced_contender_bye_player: p.forced_contender_bye_player,
        forced_leader_vs_third: p.forced_leader_vs_third,
        disallowed_leader_opponent: p.disallowed_leader_opponent,
        top4_lock_active: p.top4_lock_active,
        gibson_gets_bye: p.gibson_gets_bye,
    }
}
