from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, Client
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Profile,
    Language,
    BlogPost,
    Comment,
    CommentVote,
)
from judge.models.comment import get_user_vote_on_comment, get_top_level_comment_ids


class CommentVotingTestCase(TransactionTestCase):
    """Test cases for comment voting.

    Uses TransactionTestCase because vote_comment can raise IntegrityError
    which breaks the transaction in MySQL when using TestCase.
    """

    def setUp(self):
        cache.clear()
        self.client = Client()

        # Get or create language
        self.language, _ = Language.objects.get_or_create(
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

        # Create voter user (staff to bypass voting requirement)
        self.user = User.objects.create_user(
            username="test_user", password="password123", is_staff=True
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        # Create comment author (different from voter)
        self.author_user = User.objects.create_user(
            username="comment_author", password="password123", is_staff=True
        )
        self.author_profile, _ = Profile.objects.get_or_create(
            user=self.author_user, defaults={"language": self.language}
        )

        # Create blog and comment (authored by author, not voter)
        self.blog = BlogPost.objects.create(
            title="Test Blog",
            slug="test-blog",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        content_type = ContentType.objects.get_for_model(BlogPost)
        self.comment = Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.author_profile,
            body="Test comment",
        )

    def tearDown(self):
        cache.clear()

    def test_upvote_requires_login(self):
        """Test that voting requires authentication."""
        response = self.client.post(
            reverse("comment_upvote"),
            {"id": self.comment.id},
        )
        # Should redirect to login
        self.assertIn(response.status_code, [302, 403])

    def test_upvote_comment(self):
        """Test upvoting a comment."""
        self.client.login(username="test_user", password="password123")

        response = self.client.post(
            reverse("comment_upvote"),
            {"id": self.comment.id},
        )

        self.assertEqual(response.status_code, 200)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.score, 1)

        # Check vote was recorded
        self.assertTrue(
            CommentVote.objects.filter(
                comment=self.comment, voter=self.profile, score=1
            ).exists()
        )

    def test_downvote_comment(self):
        """Test downvoting a comment."""
        self.client.login(username="test_user", password="password123")

        response = self.client.post(
            reverse("comment_downvote"),
            {"id": self.comment.id},
        )

        self.assertEqual(response.status_code, 200)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.score, -1)

        # Check vote was recorded
        self.assertTrue(
            CommentVote.objects.filter(
                comment=self.comment, voter=self.profile, score=-1
            ).exists()
        )

    def test_cannot_double_vote(self):
        """Test that user cannot vote twice in same direction."""
        self.client.login(username="test_user", password="password123")

        # First vote
        self.client.post(reverse("comment_upvote"), {"id": self.comment.id})

        # Second vote should fail
        response = self.client.post(
            reverse("comment_upvote"),
            {"id": self.comment.id},
        )

        self.assertEqual(response.status_code, 400)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.score, 1)  # Still 1, not 2

    def test_undo_vote(self):
        """Test that voting opposite direction undoes vote."""
        self.client.login(username="test_user", password="password123")

        # Upvote
        self.client.post(reverse("comment_upvote"), {"id": self.comment.id})
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.score, 1)

        # Downvote should undo upvote
        self.client.post(reverse("comment_downvote"), {"id": self.comment.id})
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.score, 0)

        # Vote record should be deleted
        self.assertFalse(
            CommentVote.objects.filter(
                comment=self.comment, voter=self.profile
            ).exists()
        )

    def test_self_vote_prevented(self):
        """Test that users cannot vote on their own comments."""
        # Login as the comment author
        self.client.login(username="comment_author", password="password123")

        response = self.client.post(
            reverse("comment_upvote"),
            {"id": self.comment.id},
        )

        self.assertEqual(response.status_code, 400)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.score, 0)

    def test_vote_nonexistent_comment(self):
        """Test voting on a nonexistent comment returns 404."""
        self.client.login(username="test_user", password="password123")

        response = self.client.post(
            reverse("comment_upvote"),
            {"id": 99999},
        )

        self.assertEqual(response.status_code, 404)

    def test_vote_requires_post_method(self):
        """Test that voting requires POST method."""
        self.client.login(username="test_user", password="password123")

        response = self.client.get(
            reverse("comment_upvote"),
            {"id": self.comment.id},
        )

        self.assertEqual(response.status_code, 403)


class CommentVotingPermissionTestCase(TestCase):
    """Test cases for comment voting permissions (new user restrictions)."""

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

    def setUp(self):
        cache.clear()
        self.client = Client()

        # Create a non-staff user with no solved problems
        self.new_user = User.objects.create_user(
            username="new_user", password="password123", is_staff=False
        )
        self.new_profile, _ = Profile.objects.get_or_create(
            user=self.new_user, defaults={"language": self.language}
        )

        # Create blog and comment
        self.blog = BlogPost.objects.create(
            title="Test Blog",
            slug="test-blog-permission",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        content_type = ContentType.objects.get_for_model(BlogPost)
        self.comment = Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.new_profile,
            body="Test comment",
        )

    def tearDown(self):
        cache.clear()

    def test_new_user_cannot_vote(self):
        """Test that new users without solved problems cannot vote."""
        self.client.login(username="new_user", password="password123")

        response = self.client.post(
            reverse("comment_upvote"),
            {"id": self.comment.id},
        )

        # Should return 400 Bad Request (message may be in different language)
        self.assertEqual(response.status_code, 400)


class VoteCommentCacheDirtyTest(CommentVotingTestCase):
    def test_vote_dirties_user_vote_cache(self):
        """After voting, get_user_vote_on_comment must return the new state, not the stale cached 0."""
        # Prime the cache: user has no vote yet
        self.assertEqual(get_user_vote_on_comment(self.profile.id, self.comment.id), 0)

        # Upvote via the view
        self.client.login(username="test_user", password="password123")
        response = self.client.post(reverse("comment_upvote"), {"id": self.comment.id})
        self.assertEqual(response.status_code, 200)

        # Cache must reflect the new vote
        self.assertEqual(get_user_vote_on_comment(self.profile.id, self.comment.id), 1)

    def test_vote_dirties_score_sorted_list_cache(self):
        """After voting, the score-sorted top-level-comment-ids cache must be invalidated."""
        ct_id = self.comment.content_type_id
        obj_id = self.comment.object_id

        # Prime the score-sorted list cache
        get_top_level_comment_ids(ct_id, obj_id, "score", "desc")

        # Upvote via the view
        self.client.login(username="test_user", password="password123")
        self.client.post(reverse("comment_upvote"), {"id": self.comment.id})

        # Cache key must no longer be present
        from django.core.cache import cache
        from judge.caching import arg_to_str

        key = (
            f"ctlcids2:{arg_to_str(ct_id)}:{arg_to_str(obj_id)}"
            f":{arg_to_str('score')}:{arg_to_str('desc')}"
        )
        self.assertIsNone(cache.get(key))
