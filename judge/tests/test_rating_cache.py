from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.models import Contest, Language, Profile
from judge.models.contest import ContestParticipation
from judge.ratings import rate_contest
from judge.utils.users import get_contest_ratings


class RateContestCacheInvalidationTest(TestCase):
    """
    Tests that rate_contest() correctly invalidates the get_contest_ratings
    cache (prefix 'gcr2') after rating a contest.

    Regression test for: cache left stale when process dies between transaction
    commit and the dirty_multi calls (fixed by using transaction.on_commit).
    """

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
                "template": "",
            },
        )

    def _make_user(self, username):
        user = User.objects.create_user(username=username, password="pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        return profile

    def _make_contest(self, key, rate_all=True):
        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=key,
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),
            is_rated=True,
            rate_all=rate_all,
            is_visible=True,
            is_private=False,
            is_organization_private=False,
            is_in_course=False,
        )

    def test_cache_is_dirtied_after_rating(self):
        """After rate_contest(), the gcr2 cache entry must be cleared."""
        profile = self._make_user("testuser_rating")
        contest = self._make_contest("test_cache_dirty")

        ContestParticipation.objects.create(
            contest=contest,
            user=profile,
            virtual=ContestParticipation.LIVE,
            score=100,
        )

        # Populate the cache with stale data (empty list, before rating ran)
        stale = get_contest_ratings(profile.id)
        self.assertEqual(stale, [], "Cache should be empty before rating")

        # Rate the contest; on_commit fires immediately inside this context
        with self.captureOnCommitCallbacks(execute=True):
            rate_contest(contest)

        # Cache must now be dirtied — fresh call should hit DB and return 1 entry
        fresh = get_contest_ratings(profile.id)
        self.assertEqual(len(fresh), 1, "Cache should reflect the new rating record")
        self.assertEqual(fresh[0]["contest_key"], contest.key)

    def test_cache_is_dirtied_for_all_participants(self):
        """All participants in the contest must have their caches dirtied."""
        profiles = [self._make_user(f"participant_{i}") for i in range(3)]
        contest = self._make_contest("test_cache_multi")

        for i, profile in enumerate(profiles):
            ContestParticipation.objects.create(
                contest=contest,
                user=profile,
                virtual=ContestParticipation.LIVE,
                score=100 - i * 10,
            )

        # Pre-populate stale caches for all participants
        for profile in profiles:
            get_contest_ratings(profile.id)

        with self.captureOnCommitCallbacks(execute=True):
            rate_contest(contest)

        for profile in profiles:
            fresh = get_contest_ratings(profile.id)
            self.assertEqual(
                len(fresh),
                1,
                f"Cache for participant {profile.user.username} should be dirtied",
            )
