//! Maximum-weight general-graph matching.
//!
//! Shift the edge weights so the minimum is zero — which stops the
//! `max_cardinality` pass from dropping edges with negative weight — then run a
//! general-graph (non-bipartite) maximum-weight matching with
//! `max_cardinality = true`.
//!
//! The matching itself is delegated to a vendored copy of rustworkx-core's
//! `max_weight_matching` (see `src/vendor/`).

use crate::vendor::max_weight_matching::max_weight_matching;
use petgraph::graph::{NodeIndex, UnGraph};

/// Compute a maximum-weight matching over nodes `0..n`.
///
/// `edges` are `(u, v, weight)` with `u != v`. Returns the matched node-index
/// pairs, each normalized to `(min, max)` and sorted, so the output is
/// deterministic for a given input (the underlying matching is a set).
pub fn max_weight_matching_pairs(n: usize, edges: &[(usize, usize, i128)]) -> Vec<(usize, usize)> {
    if edges.is_empty() {
        return Vec::new();
    }
    // Shift so the minimum weight is zero (see module docs).
    let min_w = edges.iter().map(|&(_, _, w)| w).min().unwrap();

    let mut g: UnGraph<(), i128> = UnGraph::with_capacity(n, edges.len());
    for _ in 0..n {
        g.add_node(());
    }
    for &(u, v, w) in edges {
        g.add_edge(NodeIndex::new(u), NodeIndex::new(v), w - min_w);
    }

    // weight_fn is infallible here, so the error type is `()`.
    let matching = max_weight_matching(&g, true, |e| Ok::<i128, ()>(*e.weight()), false)
        .expect("max_weight_matching with an infallible weight function cannot fail");

    let mut pairs: Vec<(usize, usize)> = matching
        .into_iter()
        .map(|(a, b)| if a <= b { (a, b) } else { (b, a) })
        .collect();
    pairs.sort_unstable();
    pairs
}

#[cfg(test)]
mod tests {
    use super::*;

    // Path 1-2-3-4 with weights 5, 11, 5. Without max_cardinality the answer is
    // the single heaviest edge (2, 3); but the engine always runs with
    // max_cardinality = true, which prefers the 2-edge matching {(1,2),(3,4)}
    // (cardinality 2) over the heavier single edge.
    #[test]
    fn max_cardinality_prefers_more_edges() {
        let edges = [(1, 2, 5), (2, 3, 11), (3, 4, 5)];
        let pairs = max_weight_matching_pairs(5, &edges);
        assert_eq!(pairs, vec![(1, 2), (3, 4)]);
    }

    #[test]
    fn perfect_matching_on_four_nodes() {
        // Two disjoint heavy edges should both be chosen under max_cardinality.
        let edges = [(0, 1, 10), (2, 3, 10), (1, 2, 1)];
        let pairs = max_weight_matching_pairs(4, &edges);
        assert_eq!(pairs, vec![(0, 1), (2, 3)]);
    }

    #[test]
    fn empty_is_empty() {
        assert!(max_weight_matching_pairs(0, &[]).is_empty());
    }
}
