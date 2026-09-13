from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase

from judge.models import Organization, Profile
from judge.views.organization import OrganizationList


class OrganizationSearchOrderingTests(SimpleTestCase):
    def get_queryset(self, tab, order, search="school", profile=None):
        request = RequestFactory().get("/organizations/", {"organization": search})
        request.user = AnonymousUser()
        request.profile = profile
        view = OrganizationList()
        view.setup(request)
        view.current_tab = tab
        view.organization_query = search
        view.order = order

        with (
            patch(
                "judge.views.organization.ContentType.objects.get_for_model",
                return_value=SimpleNamespace(id=1),
            ),
            patch("judge.views.organization.get_all_blocked_pairs", return_value=[]),
            patch.object(Profile, "organizations") as organizations,
        ):
            organizations.values.return_value = Organization.objects.none().values("id")
            # SimpleTestCase also ensures building the result never queries the DB.
            return view.get_queryset()

    def assert_stable_ordering(self, queryset, order):
        compiler = queryset.query.get_compiler(using=queryset.db)
        ordering = [sql for _, (sql, _, _) in compiler.get_order_by()]
        self.assertEqual(len(ordering), 3)
        self.assertIn("is_community", ordering[0])
        self.assertTrue(ordering[0].endswith(" DESC"))
        self.assertTrue(
            ordering[1].endswith(" DESC" if order.startswith("-") else " ASC")
        )
        self.assertIn("id", ordering[2])
        self.assertTrue(ordering[2].endswith(" ASC"))

    def test_search_and_browsing_have_stable_ordering_on_public_tabs(self):
        for tab in ("community", "public", "private"):
            for order in ("name", "-name", "member_count", "-member_count"):
                for search in ("school", ""):
                    with self.subTest(tab=tab, order=order, search=search):
                        queryset = self.get_queryset(tab, order, search)
                        self.assert_stable_ordering(queryset, order)

    def test_personal_tabs_and_last_visit_have_stable_ordering(self):
        for tab in ("community", "public", "private", "mine", "blocked"):
            for order in ("last_visit", "-last_visit", "-member_count"):
                with self.subTest(tab=tab, order=order):
                    queryset = self.get_queryset(
                        tab, order, profile=Profile(id=1, language_id=1)
                    )
                    self.assert_stable_ordering(queryset, order)
