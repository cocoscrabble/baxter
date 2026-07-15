"""Phase 5: fixed-seed fuzz runs. Each drives a tournament through random ops,
checking invariants (including the replay-digest meta-invariant) after each step.
A failure is reproducible from its seed and its event log."""

from django.test import TestCase, tag

from tournaments.fuzz import Fuzzer


@tag("slow")
class FuzzTests(TestCase):
    def test_fixed_seeds(self):
        for seed in range(6):
            with self.subTest(seed=seed):
                Fuzzer(seed).run(steps=20, check_replay_every=10)
