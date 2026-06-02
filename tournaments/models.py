import re

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Tournament(models.Model):
    """A scrabble tournament."""

    name = models.CharField(max_length=200)
    location = models.CharField(max_length=200)
    start_date = models.DateField()
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="owned_tournaments",
    )
    editors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name="editable_tournaments",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("tournament_detail", kwargs={"pk": self.pk})

    def can_edit(self, user):
        """Check if user can edit this tournament. Anonymous users always return False."""
        if not user.is_authenticated:
            return False
        return user == self.owner or self.editors.filter(pk=user.pk).exists()

    def division_buckets(self):
        """Return divisions grouped into regular, test, and deleted lists."""
        return {
            "regular_divisions": self.divisions.filter(is_test=False),
            "test_divisions": self.divisions.filter(is_test=True),
            "deleted_divisions": Division.all_objects.filter(
                tournament=self, is_deleted=True
            ),
        }


class ActiveDivisionManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class Division(models.Model):
    """A division within a tournament."""

    name = models.CharField(max_length=100)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="divisions",
    )
    is_test = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = ActiveDivisionManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["name"]
        unique_together = ["tournament", "name"]

    def __str__(self):
        return self.name

    def soft_delete(self):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save()

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.save()

    def max_round(self):
        return self.result_slips.aggregate(
            max_round=models.Max("round")
        )["max_round"] or 0

    def pairings_by_round_pair(self):
        """Return {(round, frozenset({first_id, second_id})): Pairing} for all pairings."""
        return {
            (p.round, frozenset({p.first_id, p.second_id})): p
            for p in self.pairings.all()
        }

    def configured_round_numbers(self, default=range(1, 16)):
        """Return sorted round numbers configured in DivisionSettings, or a default range."""
        try:
            rps = self.settings.round_pairings
            return sorted({rp["round"] for rp in rps})
        except (AttributeError, KeyError, TypeError, DivisionSettings.DoesNotExist):
            return list(default)


class DivisionSettings(models.Model):
    """Settings for a division."""

    division = models.OneToOneField(
        Division,
        on_delete=models.CASCADE,
        related_name="settings",
    )
    round_pairings = models.JSONField(default=list)
    board_table_map = models.JSONField(default=list)
    # Source of truth for the round-pairings editor; round_pairings is derived
    # from it. Each block: {"pairing", "rounds", "pair_from"}.
    pairing_blocks = models.JSONField(default=list)

    def __str__(self):
        return f"Settings for {self.division}"


def next_player_number():
    """Generate the next player number by incrementing the last one lexically.

    Player numbers have an optional alpha prefix followed by digits (e.g. "A100", "100").
    Sort all existing numbers lexically, take the last, and increment the integer part.
    """
    all_numbers = list(
        Player.objects.values_list("player_number", flat=True)
    )
    if not all_numbers:
        return "1"
    all_numbers.sort()
    last = all_numbers[-1]
    m = re.match(r"^([A-Za-z]*)(\d+)$", last)
    if not m:
        return "1"
    prefix, num_str = m.groups()
    return f"{prefix}{int(num_str) + 1}"


class Player(models.Model):
    """A tournament player."""

    name = models.CharField(max_length=200)
    player_number = models.CharField(max_length=8)
    rating = models.IntegerField()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @classmethod
    def create_unique(cls, name, rating=0):
        """Create a Player with case-insensitive name uniqueness and auto-assigned number.

        Returns (player, error_message). On conflict or invalid input, player is None.
        """
        name = (name or "").strip()
        if not name:
            return None, "Name is required."
        if cls.objects.filter(name__iexact=name).exists():
            return None, f"A player named '{name}' already exists."
        try:
            rating = int(rating)
        except (ValueError, TypeError):
            rating = 0
        player = cls.objects.create(
            name=name,
            player_number=next_player_number(),
            rating=rating,
        )
        return player, None


class Entrant(models.Model):
    """A player entered in a division."""

    division = models.ForeignKey(
        Division,
        on_delete=models.CASCADE,
        related_name="entrants",
    )
    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE,
        related_name="entries",
    )
    number = models.IntegerField()

    class Meta:
        ordering = ["number"]
        unique_together = [("division", "number"), ("division", "player")]

    def __str__(self):
        return f"{self.number}: {self.player.name}"

    @property
    def name(self):
        return self.player.name


class ResultSlip(models.Model):
    """A game result slip."""

    division = models.ForeignKey(
        Division,
        on_delete=models.CASCADE,
        related_name="result_slips",
    )
    round = models.IntegerField()
    pairing = models.OneToOneField(
        "Pairing",
        on_delete=models.SET_NULL,
        related_name="result",
        null=True,
        blank=True,
    )
    winner = models.ForeignKey(
        Entrant,
        on_delete=models.CASCADE,
        related_name="wins",
    )
    winner_score = models.IntegerField()
    loser = models.ForeignKey(
        Entrant,
        on_delete=models.CASCADE,
        related_name="losses",
    )
    loser_score = models.IntegerField()
    winner_started = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        ordering = ["round"]

    @property
    def winner_name(self):
        return self.winner.player.name

    @property
    def loser_name(self):
        return self.loser.player.name

    def to_dict(self):
        return {
            "id": self.pk,
            "round": self.round,
            "winner": self.winner_id,
            "winner_score": self.winner_score,
            "loser": self.loser_id,
            "loser_score": self.loser_score,
            "winner_started": self.winner_started,
        }

    def __str__(self):
        return f"R{self.round}: {self.winner_name} {self.winner_score}-{self.loser_score} {self.loser_name}"


class RoundPairingsQuerySet(models.QuerySet):
    def revert_published_to_draft(self, round_numbers=None):
        """Move PUBLISHED rounds back to DRAFT so they can be regenerated.

        If ``round_numbers`` is provided, the change is scoped to those rounds.
        Caller is responsible for confirming none of these rounds have results.
        """
        qs = self.filter(status=RoundPairings.PUBLISHED)
        if round_numbers is not None:
            if not round_numbers:
                return 0
            qs = qs.filter(round__in=round_numbers)
        return qs.update(status=RoundPairings.DRAFT)


class RoundPairings(models.Model):
    """Lifecycle container for all pairings in a division round."""

    DRAFT = "draft"
    PUBLISHED = "published"
    IN_PROGRESS = "in_progress"
    FINISHED = "finished"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (PUBLISHED, "Published"),
        (IN_PROGRESS, "In Progress"),
        (FINISHED, "Finished"),
    ]

    division = models.ForeignKey(
        Division,
        on_delete=models.CASCADE,
        related_name="round_pairings_set",
    )
    round = models.IntegerField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=DRAFT)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = RoundPairingsQuerySet.as_manager()

    class Meta:
        unique_together = ["division", "round"]
        ordering = ["round"]

    def __str__(self):
        return f"R{self.round} ({self.get_status_display()}) - {self.division}"

    def update_status(self):
        """Recompute lifecycle status from the current count of results."""
        total = self.pairings.count()
        with_results = self.pairings.filter(result__isnull=False).count()
        if with_results == 0 and self.status == RoundPairings.IN_PROGRESS:
            self.status = RoundPairings.PUBLISHED
            self.save(update_fields=["status"])
        elif 0 < with_results < total and self.status == RoundPairings.PUBLISHED:
            self.status = RoundPairings.IN_PROGRESS
            self.save(update_fields=["status"])
        elif with_results == total and self.status in (RoundPairings.PUBLISHED, RoundPairings.IN_PROGRESS):
            self.status = RoundPairings.FINISHED
            self.save(update_fields=["status"])


class Pairing(models.Model):
    """A generated pairing for a division round."""

    division = models.ForeignKey(
        Division,
        on_delete=models.CASCADE,
        related_name="pairings",
    )
    round = models.IntegerField()
    round_pairings = models.ForeignKey(
        RoundPairings,
        on_delete=models.CASCADE,
        related_name="pairings",
        null=True,
        blank=True,
    )
    first = models.ForeignKey(
        Entrant,
        on_delete=models.CASCADE,
        related_name="pairings_as_first",
    )
    second = models.ForeignKey(
        Entrant,
        on_delete=models.CASCADE,
        related_name="pairings_as_second",
    )
    repeats = models.IntegerField(default=0)
    table = models.IntegerField(default=0)

    class Meta:
        ordering = ["round", "table"]

    def __str__(self):
        return f"R{self.round}: {self.first.name} vs {self.second.name}"


class FixedPairing(models.Model):
    """A fixed (pre-set) pairing for a division round."""

    division = models.ForeignKey(Division, on_delete=models.CASCADE, related_name="fixed_pairings")
    round_number = models.IntegerField()
    entrant1 = models.ForeignKey(Entrant, on_delete=models.CASCADE, related_name="fixed_pairings_as_first")
    entrant2 = models.ForeignKey(Entrant, on_delete=models.CASCADE, related_name="fixed_pairings_as_second")

    class Meta:
        ordering = ["round_number"]

    def __str__(self):
        return f"R{self.round_number}: {self.entrant1.name} vs {self.entrant2.name}"


class FixedTable(models.Model):
    """A fixed (pre-set) table assignment for an entrant in a division round."""

    division = models.ForeignKey(Division, on_delete=models.CASCADE, related_name="fixed_tables")
    round_number = models.IntegerField()
    entrant = models.ForeignKey(Entrant, on_delete=models.CASCADE, related_name="fixed_tables")
    table_number = models.IntegerField()

    class Meta:
        ordering = ["round_number", "table_number"]

    def __str__(self):
        return f"R{self.round_number}: {self.entrant.player.name} at table {self.table_number}"


# The bulk-editable grids. Used as the ``scope`` half of the editgrid key
# (see ``edit_key`` in views) for the optimistic-concurrency token and editing
# presence, both of which now live in the reusable ``editgrid`` app.
EDIT_SCOPES = frozenset(
    {"entrants", "results", "fixed_pairings", "fixed_tables", "board_table_map", "round_pairings"}
)
