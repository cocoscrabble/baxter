"""Run the tournament fuzzer (see tournaments/fuzz.py).

Drives random tournaments and checks invariants, including that replaying the
log reproduces the state. Everything runs in a transaction that is rolled back,
so the database is left untouched. On failure the reproducing event log is
written to a file.

    manage.py fuzz_tournament --seeds 20 --steps 40
    manage.py fuzz_tournament --seed 7 --steps 100
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from tournaments.events import export_jsonl
from tournaments.fuzz import Fuzzer


class Command(BaseCommand):
    help = "Fuzz tournaments through the command layer, checking invariants."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--seed", type=int, help="Run a single seed")
        group.add_argument(
            "--seeds", type=int, default=10, help="Run seeds 0..N-1 (default 10)"
        )
        parser.add_argument("--steps", type=int, default=30)

    def handle(self, *args, **options):
        # The test Client uses the "testserver" host.
        if "testserver" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["testserver"]

        seeds = [options["seed"]] if options["seed"] is not None else range(options["seeds"])
        failures = 0
        for seed in seeds:
            fuzzer = Fuzzer(seed)
            with transaction.atomic():
                try:
                    fuzzer.run(steps=options["steps"], check_replay_every=10)
                except Exception as exc:  # noqa: BLE001 — report any failure
                    failures += 1
                    path = f"fuzz-fail-seed{seed}.jsonl"
                    try:
                        if fuzzer.tournament is not None:
                            with open(path, "w") as fh:
                                fh.write(export_jsonl(fuzzer.tournament))
                            note = f" (log written to {path})"
                        else:
                            note = ""
                    except Exception:
                        note = ""
                    self.stderr.write(
                        self.style.ERROR(f"seed {seed} FAILED: {exc}{note}")
                    )
                else:
                    self.stdout.write(self.style.SUCCESS(f"seed {seed} ok"))
                finally:
                    transaction.set_rollback(True)

        if failures:
            self.stderr.write(self.style.ERROR(f"{failures} seed(s) failed"))
        else:
            self.stdout.write(self.style.SUCCESS("all seeds passed"))
