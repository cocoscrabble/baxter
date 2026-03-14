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
        """Check if user can edit this tournament."""
        return user == self.owner or self.editors.filter(pk=user.pk).exists()


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


class DivisionSettings(models.Model):
    """Settings for a division."""

    division = models.OneToOneField(
        Division,
        on_delete=models.CASCADE,
        related_name="settings",
    )
    round_pairings = models.JSONField(default=list)

    def __str__(self):
        return f"Settings for {self.division}"


class Player(models.Model):
    """A tournament player."""

    name = models.CharField(max_length=200)
    player_number = models.CharField(max_length=8)
    rating = models.IntegerField()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


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


class Pairing(models.Model):
    """A generated pairing for a division round."""

    division = models.ForeignKey(
        Division,
        on_delete=models.CASCADE,
        related_name="pairings",
    )
    round = models.IntegerField()
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
