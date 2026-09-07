"""The 404/403/500 pages.

Django only uses these when ``DEBUG`` is False, which is how tests run and how
production runs — with ``DEBUG`` on you get the debug page instead. So these
tests are the only thing that exercises them: there is no way to see them by
clicking around in dev.
"""

from datetime import date

from django.test import TestCase
from django.urls import reverse

from tournaments.models import Division, Tournament
from users.models import User


class NotFoundPageTests(TestCase):
    def test_a_dead_url_explains_itself_and_links_home(self):
        response = self.client.get("/tournaments/no-such-tournament/")
        self.assertEqual(response.status_code, 404)
        body = response.content.decode()
        self.assertIn("Page not found", body)
        # The way back, which is the whole point — a bare 404 leaves a visitor
        # with nothing to click.
        self.assertIn(reverse("tournament_list"), body)

    def test_it_names_the_path_that_failed(self):
        response = self.client.get("/tournaments/gone-2026/division/div-2/")
        self.assertContains(
            response, "/tournaments/gone-2026/division/div-2/", status_code=404
        )

    def test_a_deleted_tournaments_pages_are_the_case_it_speaks_to(self):
        # The report that prompted this: every tab open inside a deleted
        # tournament 404s at once, which reads as the whole site being down.
        owner = User.objects.create_user(username="o", password="p")
        tournament = Tournament.objects.create(
            name="Gone Cup", location="x", start_date=date(2026, 3, 15), owner=owner
        )
        Division.objects.create(tournament=tournament, name="Open")
        url = reverse(
            "division_entrants",
            kwargs={"tournament_slug": tournament.slug, "division_slug": "open"},
        )
        self.assertEqual(self.client.get(url).status_code, 200)

        tournament.delete()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        self.assertIn("probably been deleted", response.content.decode())


class ForbiddenPageTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner2", password="p")
        self.other = User.objects.create_user(username="other2", password="p")
        self.tournament = Tournament.objects.create(
            name="Private Cup", location="x",
            start_date=date(2026, 3, 15), owner=self.owner,
        )
        self.tournament.editors.add(self.owner)
        self.division = Division.objects.create(
            tournament=self.tournament, name="Open"
        )
        self.url = reverse(
            "division_edit_results", kwargs=self.division.slug_kwargs()
        )

    def test_a_non_editor_gets_the_page_not_a_bare_403(self):
        self.client.force_login(self.other)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)
        body = response.content.decode()
        self.assertIn("Not allowed", body)
        self.assertIn("add you as an editor", body)
        self.assertIn(reverse("tournament_list"), body)


class ServerErrorPageTests(TestCase):
    def test_it_renders_with_no_context_at_all(self):
        """Django renders 500.html with an empty Context — no request, no
        context processors. Rendering it here the way Django does is the check
        that it never grows a dependency on any of that: reading ``user`` would
        query the database from inside the error handler, which is precisely
        what has already failed when the database is the cause."""
        from django.template.loader import get_template

        html = get_template("500.html").render({})
        self.assertIn("Something went wrong", html)
        self.assertIn("/tournaments/", html)
        # No template inheritance, so nothing from the navbar can creep in.
        self.assertNotIn("nav-brand", html)
