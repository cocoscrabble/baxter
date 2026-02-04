from django.conf import settings
from django.db import models


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

    def can_edit(self, user):
        """Check if user can edit this tournament."""
        return user == self.owner or self.editors.filter(pk=user.pk).exists()
