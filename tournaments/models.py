from django.conf import settings
from django.db import models
from django.urls import reverse


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


class Division(models.Model):
    """A division within a tournament."""

    name = models.CharField(max_length=100)
    tournament = models.ForeignKey(
        Tournament,
        on_delete=models.CASCADE,
        related_name="divisions",
    )

    class Meta:
        ordering = ["name"]
        unique_together = ["tournament", "name"]

    def __str__(self):
        return self.name

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

    def __str__(self):
        return f"R{self.round}: {self.winner_name} {self.winner_score}-{self.loser_score} {self.loser_name}"
