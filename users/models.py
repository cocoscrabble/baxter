from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """Custom user model with role field for tournament management."""

    class Role(models.TextChoices):
        DIRECTOR = "director", "Director"
        SUPERVISOR = "supervisor", "Supervisor"
        ADMIN = "admin", "Admin"

    # Roles are ranked, and each level carries every power of the ones below it.
    # Compare them only through ``has_role_at_least`` — an ``==`` test against a
    # single role silently excludes the levels above it, so a role slotted into
    # the middle later would not inherit the powers it is meant to.
    ROLE_RANK = {
        Role.DIRECTOR: 0,
        Role.SUPERVISOR: 1,
        Role.ADMIN: 2,
    }

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.DIRECTOR,
    )

    class Meta:
        db_table = "users_user"

    def __str__(self):
        return self.username

    @property
    def is_admin(self):
        """Whether this user holds the Admin role (or outranks it).

        Exists for templates, which cannot call a method with an argument: the
        navbar tests this on every page. It delegates rather than comparing
        ``role`` itself, so the ranking stays defined in exactly one place.
        """
        return self.has_role_at_least(self.Role.ADMIN)

    def has_role_at_least(self, role):
        """Does this user hold ``role`` or something above it?

        A Django superuser outranks every role. An unrecognised stored role
        (one retired by a later migration) ranks below all of them.
        """
        if self.is_superuser:
            return True
        return self.ROLE_RANK.get(self.role, -1) >= self.ROLE_RANK[role]
