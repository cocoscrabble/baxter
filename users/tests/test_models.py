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
        self.assertEqual(User.Role.ADMIN, "admin")

    def test_str_returns_username(self):
        user = User.objects.create_user(username="testuser", password="testpass123")
        self.assertEqual(str(user), "testuser")

    def test_can_set_role_to_admin(self):
        user = User.objects.create_user(username="testuser", password="testpass123")
        user.role = User.Role.ADMIN
        user.save()
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.ADMIN)
