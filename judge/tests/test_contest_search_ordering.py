from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, override_settings

from judge.views.contests import (
    ContestList,
    OfficialContestList,
    RecommendedContestList,
)


class ContestSearchOrderingTests(SimpleTestCase):
    def make_view(self, view_class, search="joi16"):
        request = RequestFactory().get("/contests/", {"contest": search})
        request.user = AnonymousUser()
        request.profile = None
        request.organization = None
        request.session = {}
        view = view_class()
        view.setup(request)
        view.setup_contest_list(request)
        view.current_tab = "past"
        view.order = view.get_default_sort_order(request)
        return view

    def assert_unique_ordering(self, queryset, primary):
        compiler = queryset.query.get_compiler(using=queryset.db)
        ordering = [sql for _, (sql, _, _) in compiler.get_order_by()]
        self.assertEqual(len(ordering), 2)
        self.assertIn(primary, ordering[0])
        self.assertIn("key", ordering[1])
        self.assertTrue(ordering[1].endswith(" ASC"))

    @override_settings(ENABLE_FTS=True)
    def test_full_text_search_preserves_relevance_and_unique_tiebreaker(self):
        view = self.make_view(ContestList)
        for tab in ("current", "future", "past"):
            with self.subTest(tab=tab):
                queryset = getattr(view, "_get_%s_contests_queryset" % tab)()
                self.assert_unique_ordering(queryset, "relevance")

        official_view = self.make_view(OfficialContestList)
        self.assert_unique_ordering(official_view.get_queryset(), "relevance")

    @override_settings(ENABLE_FTS=False)
    def test_substring_search_preserves_unique_tiebreaker(self):
        view = self.make_view(ContestList)
        for tab in ("current", "future", "past"):
            with self.subTest(tab=tab):
                view.current_tab = tab
                view.order = view.get_default_sort_order(view.request)
                queryset = getattr(view, "_get_%s_contests_queryset" % tab)()
                self.assert_unique_ordering(queryset, "start_time")

        official_view = self.make_view(OfficialContestList)
        self.assert_unique_ordering(official_view.get_queryset(), "start_time")

    @override_settings(USE_ML=True)
    @patch(
        "judge.utils.contest_recommendation.get_recommended_contests_for_anonymous",
        return_value=[1, 2, 3],
    )
    def test_recommended_search_breaks_popularity_ties(self, recommendations):
        view = self.make_view(RecommendedContestList)

        self.assert_unique_ordering(view.get_queryset(), "user_count")
        recommendations.assert_called_once_with(limit=100)
