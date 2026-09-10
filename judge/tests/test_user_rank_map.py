from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Profile
from judge.utils.profile_ranks import build_profile_rank_map


class BuildProfileRankMapTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.language = Language.objects.first()

    def make_profile(self, username, **kwargs):
        user = User.objects.create_user(username=username, password="pw")
        defaults = {"language": self.language}
        defaults.update(kwargs)
        return Profile.objects.create(user=user, **defaults)

    def test_rank_uses_selected_sort_field_and_ties(self):
        first = self.make_profile("rank_perf_first", performance_points=100, points=0)
        tied_a = self.make_profile(
            "rank_perf_tied_a", performance_points=50, points=1000
        )
        tied_b = self.make_profile(
            "rank_perf_tied_b", performance_points=50, points=500
        )
        fourth = self.make_profile(
            "rank_perf_fourth", performance_points=10, points=2000
        )

        queryset = Profile.objects.filter(is_unlisted=False).order_by(
            "-performance_points", "id"
        )
        profiles = [first, tied_a, tied_b, fourth]

        with self.assertNumQueries(3):
            rank_map = build_profile_rank_map(
                queryset, profiles, "-performance_points", {"performance_points"}
            )

        self.assertEqual(
            rank_map,
            {
                first.id: 1,
                tied_a.id: 2,
                tied_b.id: 2,
                fourth.id: 4,
            },
        )

    def test_ascending_rank_matches_null_first_ordering(self):
        unrated = self.make_profile("rank_unrated", rating=None)
        lower = self.make_profile("rank_lower_rating", rating=100)
        higher = self.make_profile("rank_higher_rating", rating=200)

        queryset = Profile.objects.filter(is_unlisted=False).order_by("rating", "id")
        profiles = [unrated, lower, higher]

        with self.assertNumQueries(3):
            rank_map = build_profile_rank_map(queryset, profiles, "rating", {"rating"})

        self.assertEqual(
            rank_map,
            {
                unrated.id: 1,
                lower.id: 2,
                higher.id: 3,
            },
        )

    def test_descending_rank_places_null_after_non_null_values(self):
        higher = self.make_profile("rank_desc_higher", rating=200)
        lower = self.make_profile("rank_desc_lower", rating=100)
        unrated = self.make_profile("rank_desc_unrated", rating=None)

        queryset = Profile.objects.filter(is_unlisted=False).order_by("-rating", "id")
        with self.assertNumQueries(3):
            rank_map = build_profile_rank_map(
                queryset, [higher, lower, unrated], "-rating", {"rating"}
            )

        self.assertEqual(
            rank_map,
            {
                higher.id: 1,
                lower.id: 2,
                unrated.id: 3,
            },
        )

    def test_combined_counts_use_one_query_for_large_pages(self):
        first = self.make_profile("rank_combined_first", performance_points=100)
        second = self.make_profile("rank_combined_second", performance_points=50)
        queryset = Profile.objects.filter(is_unlisted=False).order_by(
            "-performance_points", "id"
        )

        with self.assertNumQueries(1):
            rank_map = build_profile_rank_map(
                queryset,
                [first, second],
                "-performance_points",
                {"performance_points"},
                combine_counts=True,
            )

        self.assertEqual(rank_map, {first.id: 1, second.id: 2})
