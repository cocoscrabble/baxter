"""The admin landing page: the navbar entry and the list of everything gated.

Two things are being pinned here. The access matrix, because this page is the
signpost to every site-wide power and must not be a signpost for a director. And
**completeness** — the page's whole reason to exist is that admin pages were
reachable only by typing a URL, so a new one that never gets listed here would
recreate exactly the problem it was built to fix.
"""

from django.test import TestCase
from django.urls import URLPattern, reverse

from tournaments.models import Player, RosterSync
from tournaments.views import IsAdminMixin
from users.models import User


def admin_only_urls():
    """Every URL in tournaments/ whose view is gated on IsAdminMixin.

    Read off the URLconf rather than listed by hand: a list would go stale on
    exactly the commit that adds a page and forgets to link it, which is the
    case this exists to catch.
    """
    from tournaments import urls

    found = {}
    for pattern in urls.urlpatterns:
        if not isinstance(pattern, URLPattern):
            continue
        view_class = getattr(pattern.callback, "view_class", None)
        if view_class and issubclass(view_class, IsAdminMixin):
            found[pattern.name] = reverse(pattern.name)
    return found


class AccessTests(TestCase):
    def setUp(self):
        self.url = reverse("admin_index")
        self.admin = User.objects.create_user(
            username="admin-idx", password="pw", role="admin"
        )
        self.director = User.objects.create_user(
            username="td-idx", password="pw", role="director"
        )
        self.supervisor = User.objects.create_user(
            username="sup-idx", password="pw", role="supervisor"
        )

    def test_an_admin_gets_the_page(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_superuser_outranks_the_role(self):
        root = User.objects.create_superuser(username="root-idx", password="pw")
        self.client.force_login(root)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_director_is_refused(self):
        self.client.force_login(self.director)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_supervisor_is_refused(self):
        # A supervisor has director powers everywhere, not admin ones.
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_anonymous_visitor_is_sent_to_log_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])


class NavbarTests(TestCase):
    """The link exists so the page is reachable without knowing the URL."""

    def setUp(self):
        self.home = reverse("tournament_list")

    def test_an_admin_sees_it(self):
        self.client.force_login(
            User.objects.create_user(username="a-nav", password="pw", role="admin")
        )
        self.assertContains(self.client.get(self.home), reverse("admin_index"))

    def test_a_director_does_not(self):
        self.client.force_login(
            User.objects.create_user(username="d-nav", password="pw", role="director")
        )
        self.assertNotContains(self.client.get(self.home), reverse("admin_index"))

    def test_an_anonymous_visitor_does_not(self):
        self.assertNotContains(self.client.get(self.home), reverse("admin_index"))

    def test_it_is_on_every_page_not_just_the_home_page(self):
        self.client.force_login(
            User.objects.create_user(username="a-nav2", password="pw", role="admin")
        )
        response = self.client.get(reverse("roster_import"))
        self.assertContains(response, reverse("admin_index"))


class CompletenessTests(TestCase):
    def setUp(self):
        self.client.force_login(
            User.objects.create_user(username="a-cov", password="pw", role="admin")
        )
        self.page = self.client.get(reverse("admin_index"))

    def test_every_admin_only_page_is_listed(self):
        listed = self.page.content.decode()
        for name, url in admin_only_urls().items():
            if name == "admin_index":
                continue
            with self.subTest(view=name):
                self.assertIn(
                    url, listed,
                    f"{name} is admin-only but not linked from /manage/, so the "
                    f"only way to reach it is to know the URL",
                )

    def test_there_is_something_to_check(self):
        # Guards the guard: a reverse() or mixin rename that silently found
        # nothing would make the test above pass by doing nothing.
        self.assertGreaterEqual(len(admin_only_urls()), 4)

    def test_the_django_admin_is_linked_too(self):
        # Not IsAdminMixin-gated (it is Django's own staff flag), so the sweep
        # above cannot see it, but it is an admin-only endpoint all the same.
        self.assertContains(self.page, reverse("admin:index"))


class RosterStatusTests(TestCase):
    """The page carries the roster's state, not just a link to it."""

    def setUp(self):
        self.client.force_login(
            User.objects.create_user(username="a-st", password="pw", role="admin")
        )
        self.url = reverse("admin_index")

    def test_a_waiting_guest_is_flagged(self):
        Player.objects.create(
            name="Joe Thorngren", player_number="T-4", rating=0, is_provisional=True
        )
        RosterSync.objects.create(
            source=RosterSync.SCHEDULED, ok=True,
            pending=[{
                "local_number": "T-4", "roster_number": "0301",
                "name": "Joe Thorngren",
                "row": {"player_number": "0301", "name": "Joe Thorngren",
                        "rating": 1400, "deviation": None, "career_games": 0,
                        "last_played": None},
            }],
        )
        response = self.client.get(self.url)
        self.assertContains(response, "need")
        self.assertContains(response, "confirming")

    def test_a_failing_scheduled_pull_is_flagged(self):
        RosterSync.objects.create(
            source=RosterSync.SCHEDULED, ok=False,
            error="The central database rejected the token.",
        )
        self.assertContains(self.client.get(self.url), "rejected the token")

    def test_a_healthy_roster_says_nothing_alarming(self):
        RosterSync.objects.create(source=RosterSync.SCHEDULED, ok=True, unchanged=244)
        response = self.client.get(self.url)
        self.assertNotContains(response, "confirming")
        self.assertNotContains(response, "failed")
