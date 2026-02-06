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


class Player(models.Model):
    """A tournament player."""

    name = models.CharField(max_length=200)
    player_number = models.CharField(max_length=8)
    rating = models.IntegerField()

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
