from django.test import TestCase

from users.forms import CustomUserCreationForm, ProfileForm
from users.models import User


class CustomUserCreationFormTests(TestCase):
    def test_valid_form(self):
        form = CustomUserCreationForm(data={
            "username": "newuser",
            "email": "newuser@example.com",
            "password1": "testpass123!",
            "password2": "testpass123!",
        })
        self.assertTrue(form.is_valid())

    def test_email_required(self):
        form = CustomUserCreationForm(data={
            "username": "newuser",
            "email": "",
            "password1": "testpass123!",
            "password2": "testpass123!",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_password_mismatch(self):
        form = CustomUserCreationForm(data={
            "username": "newuser",
            "email": "newuser@example.com",
            "password1": "testpass123!",
            "password2": "differentpass!",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("password2", form.errors)

    def test_creates_user_with_default_role(self):
        form = CustomUserCreationForm(data={
            "username": "newuser",
            "email": "newuser@example.com",
            "password1": "testpass123!",
            "password2": "testpass123!",
        })
        self.assertTrue(form.is_valid())
        user = form.save()
        self.assertEqual(user.role, User.Role.DIRECTOR)


class ProfileFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )

    def test_valid_form(self):
        form = ProfileForm(data={
            "username": "updateduser",
            "email": "updated@example.com",
            "first_name": "Test",
            "last_name": "User",
        }, instance=self.user)
        self.assertTrue(form.is_valid())

    def test_updates_user(self):
        form = ProfileForm(data={
            "username": "updateduser",
            "email": "updated@example.com",
            "first_name": "Test",
            "last_name": "User",
        }, instance=self.user)
        self.assertTrue(form.is_valid())
        user = form.save()
        self.assertEqual(user.username, "updateduser")
        self.assertEqual(user.email, "updated@example.com")
        self.assertEqual(user.first_name, "Test")
        self.assertEqual(user.last_name, "User")
