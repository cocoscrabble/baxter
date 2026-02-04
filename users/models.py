from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model with role field for tournament management."""

    class Role(models.TextChoices):
        DIRECTOR = "director", "Director"
        ADMIN = "admin", "Admin"

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.DIRECTOR,
    )

    class Meta:
        db_table = "users_user"

    def __str__(self):
        return self.username
