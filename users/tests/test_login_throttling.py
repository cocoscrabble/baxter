"""Login throttling (django-axes), as configured in settings.

What is pinned is the policy, not axes itself: the lockout is keyed on username
and address *together*, and the address is read so it cannot be forged.
"""

from django.test import TestCase, override_settings
from django.urls import reverse

from users.models import User


@override_settings(AXES_ENABLED=True)
class LoginThrottlingTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="td", password="right-pw")
        User.objects.create_user(username="other", password="right-pw")
        self.url = reverse("login")

    def attempt(self, username="td", password="wrong", ip="203.0.113.5", xff=None):
        extra = {"REMOTE_ADDR": "172.17.0.1"}
        extra["HTTP_X_FORWARDED_FOR"] = xff if xff is not None else ip
        return self.client.post(
            self.url, {"username": username, "password": password}, **extra
        )

    def fail(self, times, **kwargs):
        for _ in range(times):
            self.attempt(**kwargs)

    def test_the_right_password_is_refused_once_locked_out(self):
        self.fail(5)
        response = self.attempt(password="right-pw")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Too many login attempts", status_code=429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_below_the_limit_the_right_password_still_works(self):
        self.fail(4)
        response = self.attempt(password="right-pw")
        self.assertEqual(response.status_code, 302)

    def test_another_director_on_the_same_wifi_is_unaffected(self):
        self.fail(5)
        self.assertEqual(self.attempt(username="other", password="right-pw").status_code, 302)

    def test_the_same_director_elsewhere_is_unaffected(self):
        # Otherwise anyone could lock a director out by guessing at their name.
        self.fail(5)
        response = self.attempt(password="right-pw", ip="198.51.100.9")
        self.assertEqual(response.status_code, 302)

    def test_a_forged_forwarded_for_does_not_escape_the_lockout(self):
        # nginx appends the real address last; what the client sent sits left of it.
        self.fail(5)
        response = self.attempt(password="right-pw", xff="1.2.3.4, 203.0.113.5")
        self.assertEqual(response.status_code, 429)
