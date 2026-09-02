from django.test import TestCase

from users.models import User


class UserModelTests(TestCase):
    def test_create_user(self):
        user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )
        self.assertEqual(user.username, "testuser")
        self.assertEqual(user.email, "test@example.com")
        self.assertTrue(user.check_password("testpass123"))

    def test_default_role_is_director(self):
        user = User.objects.create_user(username="testuser", password="testpass123")
        self.assertEqual(user.role, User.Role.DIRECTOR)

    def test_role_choices(self):
        self.assertEqual(User.Role.DIRECTOR, "director")
        self.assertEqual(User.Role.SUPERVISOR, "supervisor")
        self.assertEqual(User.Role.ADMIN, "admin")

    def test_roles_are_ranked_director_supervisor_admin(self):
        self.assertLess(
            User.ROLE_RANK[User.Role.DIRECTOR], User.ROLE_RANK[User.Role.SUPERVISOR]
        )
        self.assertLess(
            User.ROLE_RANK[User.Role.SUPERVISOR], User.ROLE_RANK[User.Role.ADMIN]
        )

    def test_has_role_at_least_reaches_down_not_up(self):
        supervisor = User.objects.create_user(
            username="sup", password="pw", role=User.Role.SUPERVISOR
        )
        self.assertTrue(supervisor.has_role_at_least(User.Role.DIRECTOR))
        self.assertTrue(supervisor.has_role_at_least(User.Role.SUPERVISOR))
        self.assertFalse(supervisor.has_role_at_least(User.Role.ADMIN))

    def test_superuser_outranks_every_role(self):
        root = User.objects.create_superuser(username="root", password="pw")
        self.assertEqual(root.role, User.Role.DIRECTOR)
        self.assertTrue(root.has_role_at_least(User.Role.ADMIN))

    def test_str_returns_username(self):
        user = User.objects.create_user(username="testuser", password="testpass123")
        self.assertEqual(str(user), "testuser")

    def test_can_set_role_to_admin(self):
        user = User.objects.create_user(username="testuser", password="testpass123")
        user.role = User.Role.ADMIN
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.ADMIN)
