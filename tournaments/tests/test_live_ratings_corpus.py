"""The projection must agree with the real engine on real tournaments.

Every other test here checks that the projection *assembles* its inputs the way
the official replay does. This one checks the thing that actually matters: that
the answer comes out the same. It loads a real tournament from the ratings
repo's results corpus, rates it two ways — through ``coco_ratings``' own
``Tournament`` and through Baxter's ``project_ratings`` — and demands they
agree exactly.

Skipped unless ``../ratings`` is checked out beside Baxter, since the corpus is
that repo's data and CI has only the packaged library. Tagged ``slow``: it
builds a full division per tournament.

**What it cannot see**, and what covers those instead: the corpus files carry no
last-played dates and no byes, so deviation ageing and bye handling are
invisible here — every player ages to the maximum deviation on both sides, and
there is no bye row to drop. ``test_live_ratings`` covers both directly. This
test is about the rating math agreeing; that one is about the inputs.

Run against the whole corpus with ``COCO_RATINGS_CORPUS=all``; by default it
uses a handful chosen to cover the cases that are easy to get wrong — an
all-rated field, unrated players, and a tournament whose ratings file carries
the *output* header rather than the input one.
"""

import csv
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from django.test import TestCase, tag

from tournaments.live_ratings import project_ratings
from tournaments.models import (
    Division,
    Entrant,
    Pairing,
    Player,
    ResultSlip,
    RoundPairings,
    Tournament,
)
from users.models import User

CORPUS = Path(__file__).resolve().parents[2].parent / "ratings"
RESULTS = CORPUS / "results"

# (slug, date). Chosen for coverage, not size:
#   3m-2022        — every player rated
#   atx-2022       — one unrated player, so calc_initial_ratings runs
#   austin-atx2024 — several unrated players converging against each other
#   boston-2022    — ratings file carries the output header (Name,Record,...)
DEFAULT_CASES = [
    ("3m-2022", date(2022, 5, 21)),
    ("atx-2022", date(2022, 3, 12)),
    ("austin-atx2024", date(2024, 3, 9)),
    ("boston-2022", date(2022, 4, 23)),
]


def _numeric_rounds(path):
    """Whether every round in the file is a number.

    A few older files label playoff rounds by name ("Semifinal"). The engine
    does not care — it never does arithmetic on the round — but Baxter's
    ``RoundPairings.round`` is an integer and no Baxter division produces such a
    file, so there is nothing to compare.
    """
    for row in _read_results(path):
        try:
            int(row[1])
        except ValueError:
            return False
    return True


def _cases():
    if os.environ.get("COCO_RATINGS_CORPUS") != "all":
        return DEFAULT_CASES
    rows = {}
    with open(CORPUS / "data" / "tournaments.csv", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            slug, when = row["Filename"].strip(), row["Date"].strip()
            results = RESULTS / f"{slug}-results.csv"
            # Both halves are needed: a few tournaments have results but no
            # pre-tournament ratings file, and there is nothing to seed from.
            ratings = RESULTS / f"{slug}-ratings.csv"
            if (
                slug and when and results.exists() and ratings.exists()
                and _numeric_rounds(results)
            ):
                rows[slug] = date.fromisoformat(when)
    return sorted(rows.items())


def _read_ratings(path):
    """Pre-tournament ratings, read positionally.

    ``CSVRatingsFileReader`` does the same: the header varies across the corpus
    — some files carry the results *output* header — but column 0 is always the
    name and column 1 the rating.
    """
    ratings = {}
    with open(path, encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        next(reader)
        for row in reader:
            if row and row[0].strip():
                value = row[1].strip()
                ratings[row[0].strip()] = int(value) if value else 0
    return ratings


def _read_results(path):
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if row and row[1].strip().lower() != "round":
                rows.append(row)
    return rows


@unittest.skipUnless(RESULTS.is_dir(), "../ratings is not checked out")
@tag("slow")
class ProjectionMatchesEngineTests(TestCase):
    maxDiff = None

    def setUp(self):
        self.owner = User.objects.create_user(username="corpus", password="pw")
        # Every tournament in one test method shares a transaction, and player
        # numbers are globally unique — so the counter never restarts.
        self._minted = 0

    def _number(self):
        self._minted += 1
        return f"C{self._minted:05d}"

    def _engine(self, slug, when):
        """What the official engine computes for this tournament in isolation.

        Fed a bye-free copy of the results, because some older files carry
        literal ``Bye`` rows — the previous program's convention for an odd
        field — and a Baxter export never does. Left in, the engine rates a
        phantom "Bye" player and counts the bye toward everyone's record, so the
        two sides would differ on games played while agreeing on every rating.
        Stripping them makes it an even comparison of the same games.
        """
        from coco_ratings.rating import Tournament as EngineTournament

        t = EngineTournament(
            str(RESULTS / f"{slug}-ratings.csv"),
            self._bye_free_results(slug),
            slug,
            datetime.combine(when, datetime.min.time()),
        )
        t.calc_ratings()
        return {
            p.name: (
                round(p.init_rating), round(p.new_rating),
                round(p.new_rating_deviation, 2),
                p.wins, p.losses, p.spread, len(p.games), p.is_unrated,
            )
            for section in t.sections
            for p in section.get_players()
        }

    def _baxter(self, slug, when):
        """The same tournament loaded into Baxter and projected.

        Entrants are seeded exactly as the engine seeds players: the rating from
        the ratings file, no deviation, no career history.

        A "Bye" row in an older file is the *previous* program's convention for
        an odd field; Baxter has a real bye entrant instead. Mapping it onto one
        keeps the two sides comparable — otherwise the engine rates a phantom
        player that Baxter correctly refuses to.
        """
        ratings = _read_ratings(RESULTS / f"{slug}-ratings.csv")
        rows = _read_results(RESULTS / f"{slug}-results.csv")

        tournament = Tournament.objects.create(
            name=slug, location="X", start_date=when, owner=self.owner
        )
        division = Division.objects.create(tournament=tournament, name="Open")

        names = []
        for row in rows:
            for name in (row[2].strip(), row[4].strip()):
                if name not in names and name.casefold() != "bye":
                    names.append(name)
        entrants = {}
        for i, name in enumerate(names, start=1):
            player = Player.objects.create(
                name=name, player_number=self._number(), rating=ratings.get(name, 0)
            )
            entrants[name] = Entrant.enter(division, player, i)

        by_round = {}
        for row in rows:
            by_round.setdefault(int(row[1]), []).append(row)
        for round_num, games in sorted(by_round.items()):
            rp = RoundPairings.objects.create(
                division=division, round=round_num, status=RoundPairings.FINISHED
            )
            for table, row in enumerate(games, start=1):
                if row[4].strip().casefold() == "bye":
                    continue
                winner, loser = entrants[row[2].strip()], entrants[row[4].strip()]
                pairing = Pairing.objects.create(
                    division=division, round=round_num, round_pairings=rp,
                    first=winner, second=loser, table=table,
                )
                ResultSlip.objects.create(
                    division=division, round=round_num, pairing=pairing,
                    winner=winner, winner_score=int(row[3]),
                    loser=loser, loser_score=int(row[5]), winner_started=True,
                )

        return {
            p.name: (
                p.old_rating, p.new_rating, p.new_deviation,
                p.wins, p.losses, p.spread, p.games, p.was_unrated,
            )
            for p in project_ratings(division).values()
        }

    def _bye_free_results(self, slug):
        """Path to the results file with any ``Bye`` rows removed."""
        source = RESULTS / f"{slug}-results.csv"
        rows = list(csv.reader(source.open(encoding="utf-8-sig")))
        kept = [
            r for r in rows
            if not (len(r) > 4 and r[4].strip().casefold() == "bye")
        ]
        if len(kept) == len(rows):
            return str(source)
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".csv", delete=False, newline=""
        )
        self.addCleanup(os.unlink, handle.name)
        with handle:
            csv.writer(handle).writerows(kept)
        return handle.name

    def test_the_projection_matches_the_engine(self):
        for slug, when in _cases():
            with self.subTest(tournament=slug):
                expected = self._engine(slug, when)
                actual = self._baxter(slug, when)
                self.assertEqual(
                    actual, expected,
                    f"{slug}: the projection disagrees with the rating engine",
                )
