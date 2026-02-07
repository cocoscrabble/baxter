from django.core.management.base import BaseCommand

from users.models import User


class Command(BaseCommand):
    help = "Create a default user for testing"

    def handle(self, *args, **options):
        username = "emsworth"

        if User.objects.filter(username=username).exists():
            self.stdout.write(self.style.WARNING(f"User '{username}' already exists"))
            return

        user = User.objects.create_user(
            username=username,
            password="emsworth",
            first_name="Lord",
            last_name="Emsworth",
            role=User.Role.DIRECTOR,
        )

        self.stdout.write(self.style.SUCCESS(f"Created user: {user.username}"))
        self.stdout.write(f"  Name: {user.first_name} {user.last_name}")
        self.stdout.write(f"  Role: {user.get_role_display()}")
        self.stdout.write(f"  Password: emsworth")
