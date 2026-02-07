from django.test import Client, TestCase
from django.urls import reverse

from users.models import User


class RegisterViewTests(TestCase):
    def test_get_register_page(self):
        response = self.client.get(reverse("register"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/register.html")

    def test_register_creates_user(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newuser",
                "email": "newuser@example.com",
                "password1": "testpass123!",
                "password2": "testpass123!",
            },
        )
        self.assertRedirects(response, reverse("login"))
        self.assertTrue(User.objects.filter(username="newuser").exists())

    def test_register_invalid_shows_errors(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newuser",
                "email": "",
                "password1": "testpass123!",
                "password2": "testpass123!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response.context["form"], "email", "This field is required.")


class LoginViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )

    def test_get_login_page(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/login.html")

    def test_login_success(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": "testuser",
                "password": "testpass123",
            },
        )
        # "/" redirects to tournament_list, so don't follow
        self.assertRedirects(response, "/", fetch_redirect_response=False)

    def test_login_invalid_credentials(self):
        response = self.client.post(
            reverse("login"),
            {
                "username": "testuser",
                "password": "wrongpassword",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password")


class LogoutViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
        )

    def test_logout(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.post(reverse("logout"))
        # "/" redirects to tournament_list, so don't follow
        self.assertRedirects(response, "/", fetch_redirect_response=False)


class ProfileViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpass123",
        )

    def test_profile_requires_login(self):
        response = self.client.get(reverse("profile"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('profile')}")

    def test_get_profile_page(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/profile.html")

    def test_update_profile(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.post(
            reverse("profile"),
            {
                "username": "updateduser",
                "email": "updated@example.com",
                "first_name": "Test",
                "last_name": "User",
            },
        )
        self.assertRedirects(response, reverse("profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "updateduser")
