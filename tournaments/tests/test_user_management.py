"""The admin's user list and set-password page.

The rule worth pinning is who may reset whom: setting a password takes the
account over, so it must never reach an account ranked at or above the actor's.
"""

from django.test import TestCase
from django.urls import reverse

from users.models import User


class UserListTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="adm", password="pw", role="admin"
        )
        self.director = User.objects.create_user(
            username="td", email="td@example.com", password="pw", role="director"
        )
        self.url = reverse("user_list")

    def test_a_director_is_refused(self):
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_lists_usernames_and_emails(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertContains(response, "td@example.com")
        self.assertContains(response, reverse("user_set_password", args=[self.director.pk]))

    def test_offers_no_reset_on_accounts_it_would_refuse(self):
        other_admin = User.objects.create_user(username="adm2", password="pw", role="admin")
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertNotContains(response, reverse("user_set_password", args=[other_admin.pk]))
        self.assertNotContains(response, reverse("user_set_password", args=[self.admin.pk]))


class SetPasswordTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="adm", password="pw", role="admin")
        self.director = User.objects.create_user(username="td", password="old-pw", role="director")

    def url(self, user):
        return reverse("user_set_password", args=[user.pk])

    def post(self, user, password="a-Sturdy-new-pw-42", confirm=None):
        return self.client.post(self.url(user), {
            "new_password1": password,
            "new_password2": confirm if confirm is not None else password,
        })

    def test_an_admin_sets_a_directors_password(self):
        self.client.force_login(self.admin)
        response = self.post(self.director)
        self.assertRedirects(response, reverse("user_list"))
        self.director.refresh_from_db()
        self.assertTrue(self.director.check_password("a-Sturdy-new-pw-42"))

    def test_a_mismatch_changes_nothing(self):
        self.client.force_login(self.admin)
        response = self.post(self.director, confirm="something-else-99")
        self.assertEqual(response.status_code, 200)
        self.director.refresh_from_db()
        self.assertTrue(self.director.check_password("old-pw"))

    def test_a_director_cannot_reset_anyone(self):
        other = User.objects.create_user(username="td2", password="pw")
        self.client.force_login(self.director)
        self.assertEqual(self.post(other).status_code, 403)
        # And cannot tell a missing account from a forbidden one.
        self.assertEqual(
            self.client.get(reverse("user_set_password", args=[99999])).status_code, 403
        )

    def test_a_supervisor_below_an_admin_can_be_reset(self):
        supervisor = User.objects.create_user(username="sup", password="pw", role="supervisor")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url(supervisor)).status_code, 200)

    def test_an_admin_cannot_reset_an_equal_or_higher_account(self):
        refused = [
            User.objects.create_user(username="adm2", password="pw", role="admin"),
            User.objects.create_superuser(username="root", password="pw"),
            User.objects.create_user(username="staff", password="pw", is_staff=True),
            self.admin,
        ]
        self.client.force_login(self.admin)
        for target in refused:
            with self.subTest(target=target.username):
                self.assertEqual(self.post(target).status_code, 403)
                target.refresh_from_db()
                self.assertFalse(target.check_password("a-Sturdy-new-pw-42"))

    def test_a_superuser_can_reset_an_admin(self):
        root = User.objects.create_superuser(username="root", password="pw")
        self.client.force_login(root)
        self.assertRedirects(self.post(self.admin), reverse("user_list"))
