from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, SimpleTestCase, override_settings

from judge.views.problem import ProblemFeed


class ProblemFeedSearchOrderingTests(SimpleTestCase):
    def get_search_ordering(self, search):
        request = RequestFactory().get("/problems/feed/", {"search": search})
        request.user = AnonymousUser()
        request.profile = None
        request.organization = None
        request.session = {}
        request.LANGUAGE_CODE = "en"
        view = ProblemFeed(feed_type="for_you")
        view.setup(request)
        view.setup_problem_list(request)
        queryset = view.get_queryset()
        compiler = queryset.query.get_compiler(using=queryset.db)
        return [sql for _, (sql, _, _) in compiler.get_order_by()]

    @override_settings(ENABLE_FTS=True)
    def test_full_text_search_breaks_relevance_ties_by_unique_id(self):
        ordering = self.get_search_ordering("joi16")

        self.assertEqual(len(ordering), 2)
        self.assertIn("MATCH(", ordering[0])
        self.assertTrue(ordering[0].endswith(" DESC"))
        self.assertIn("id", ordering[1])
        self.assertTrue(ordering[1].endswith(" ASC"))

    @override_settings(ENABLE_FTS=False)
    def test_substring_search_has_unique_ordering(self):
        ordering = self.get_search_ordering("joi16")

        self.assertEqual(len(ordering), 1)
        self.assertIn("id", ordering[0])
        self.assertTrue(ordering[0].endswith(" ASC"))

    @override_settings(ENABLE_FTS=True)
    def test_empty_search_has_unique_ordering(self):
        for search in ("", "  "):
            with self.subTest(search=search):
                ordering = self.get_search_ordering(search)

                self.assertEqual(len(ordering), 1)
                self.assertIn("id", ordering[0])
                self.assertTrue(ordering[0].endswith(" ASC"))
