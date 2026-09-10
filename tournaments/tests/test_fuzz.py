"""Phase 5: fixed-seed fuzz runs. Each drives a tournament through random ops,
checking invariants (including the replay-digest meta-invariant) after each step.
A failure is reproducible from its seed and its event log."""

from django.test import TestCase, tag

from tournaments.fuzz import Fuzzer


@tag("slow")
class FuzzTests(TestCase):
    def test_fixed_seeds(self):
        # Deeper than it was, because depth is what finds things: the double
        # pairing a playoff could land on an already-printed round needed a
        # schedule change, a publish and a playoff, twenty-odd steps in, and no
        # twenty-step run had ever produced it. Six seeds at 35 steps costs
        # about ten seconds more than six at 20.
        for seed in range(6):
            with self.subTest(seed=seed):
                Fuzzer(seed).run(steps=35, check_replay_every=10)
