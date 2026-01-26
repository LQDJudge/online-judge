import time

from django.core.cache import cache
from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Profile,
    Language,
    BlogPost,
    Comment,
)


class TopLevelCommentsViewTestCase(TestCase):
    """Test cases for TopLevelCommentsView."""

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

        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.blog = BlogPost.objects.create(
            title="Test Blog",
            slug="test-blog",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        self.content_type = ContentType.objects.get_for_model(BlogPost)

    def tearDown(self):
        cache.clear()

    def test_get_comments_for_blog(self):
        """Test fetching comments for a blog post."""
        Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test comment",
        )

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test comment")

    def test_get_comments_empty(self):
        """Test fetching comments when none exist."""
        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
            },
        )

        self.assertEqual(response.status_code, 200)

    def test_get_comments_missing_params(self):
        """Test that missing params returns error."""
        response = self.client.get(reverse("get_comments"))

        self.assertEqual(response.status_code, 400)

    def test_get_comments_hidden_excluded(self):
        """Test that hidden comments are excluded."""
        Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Visible comment",
            hidden=False,
        )
        Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Hidden comment",
            hidden=True,
        )

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible comment")
        self.assertNotContains(response, "Hidden comment")

    def test_get_comments_sort_by_time_desc(self):
        """Test sorting comments by time descending (most recent first)."""
        comment1 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="First comment",
        )
        time.sleep(0.01)  # Ensure different timestamps
        comment2 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Second comment",
        )

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "sort_by": "time",
                "sort_order": "desc",
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Second comment should appear before first (most recent first)
        second_pos = content.find("Second comment")
        first_pos = content.find("First comment")
        self.assertLess(second_pos, first_pos)

    def test_get_comments_sort_by_time_asc(self):
        """Test sorting comments by time ascending (oldest first)."""
        comment1 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="First comment",
        )
        time.sleep(0.01)
        comment2 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Second comment",
        )

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "sort_by": "time",
                "sort_order": "asc",
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # First comment should appear before second (oldest first)
        first_pos = content.find("First comment")
        second_pos = content.find("Second comment")
        self.assertLess(first_pos, second_pos)

    def test_get_comments_sort_by_score(self):
        """Test sorting comments by score."""
        comment1 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Low score comment",
        )
        comment2 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="High score comment",
        )
        # Give comment2 a higher score
        comment2.score = 10
        comment2.save()

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "sort_by": "score",
                "sort_order": "desc",
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # High score comment should appear before low score
        high_pos = content.find("High score comment")
        low_pos = content.find("Low score comment")
        self.assertLess(high_pos, low_pos)


class RepliesViewTestCase(TestCase):
    """Test cases for RepliesView."""

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

        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.blog = BlogPost.objects.create(
            title="Test Blog",
            slug="test-blog",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        self.content_type = ContentType.objects.get_for_model(BlogPost)

    def tearDown(self):
        cache.clear()

    def test_get_replies(self):
        """Test fetching replies to a comment."""
        parent = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )
        Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )

        response = self.client.get(
            reverse("comment_get_replies"),
            {"id": parent.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reply comment")

    def test_get_replies_missing_id(self):
        """Test that missing id returns error."""
        response = self.client.get(reverse("comment_get_replies"))

        self.assertEqual(response.status_code, 400)

    def test_get_replies_nonexistent_comment(self):
        """Test that nonexistent comment returns 404."""
        response = self.client.get(
            reverse("comment_get_replies"),
            {"id": 99999},
        )

        self.assertEqual(response.status_code, 404)


class CommentContentViewTestCase(TestCase):
    """Test cases for CommentContent view."""

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

        self.user = User.objects.create_user(
            username="test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.blog = BlogPost.objects.create(
            title="Test Blog",
            slug="test-blog",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        self.content_type = ContentType.objects.get_for_model(BlogPost)

        self.comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test comment",
        )

    def tearDown(self):
        cache.clear()

    def test_get_comment_content(self):
        """Test fetching a single comment's content."""
        response = self.client.get(
            reverse("comment_content", kwargs={"id": self.comment.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test comment")

    def test_get_nonexistent_comment(self):
        """Test that nonexistent comment returns 404."""
        response = self.client.get(
            reverse("comment_content", kwargs={"id": 99999}),
        )

        self.assertEqual(response.status_code, 404)


class HighlightedCommentsTestCase(TestCase):
    """Test cases for highlighted/target comments feature."""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3HC",
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

        self.user = User.objects.create_user(
            username="test_highlight_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.blog = BlogPost.objects.create(
            title="Test Blog Highlight",
            slug="test-blog-highlight",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        self.content_type = ContentType.objects.get_for_model(BlogPost)

    def tearDown(self):
        cache.clear()

    def test_target_comment_top_level(self):
        """Test that target_comment shows the specified top-level comment first."""
        # Create multiple comments
        comment1 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="First comment",
        )
        comment2 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Second comment",
        )
        comment3 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Third comment",
        )

        # Request with target_comment pointing to comment1
        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "target_comment": comment1.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Target comment should appear in the response
        self.assertIn("First comment", content)
        # Check that target comment appears before others
        first_pos = content.find("First comment")
        second_pos = content.find("Second comment")
        third_pos = content.find("Third comment")
        self.assertLess(first_pos, second_pos)
        self.assertLess(first_pos, third_pos)

    def test_target_comment_reply(self):
        """Test that target_comment shows the path from root to target."""
        # Create parent comment
        parent = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )
        # Create reply
        reply = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )
        # Create another top-level comment
        other = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Other comment",
        )

        # Request with target_comment pointing to the reply
        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "target_comment": reply.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # Both parent and reply should appear (the path)
        self.assertIn("Parent comment", content)
        self.assertIn("Reply comment", content)
        # Parent should appear before reply (path order: root -> target)
        parent_pos = content.find("Parent comment")
        reply_pos = content.find("Reply comment")
        self.assertLess(parent_pos, reply_pos)

    def test_target_comment_nonexistent(self):
        """Test that nonexistent target_comment is handled gracefully."""
        Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Regular comment",
        )

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "target_comment": 99999,
            },
        )

        # Should still return 200 with regular comments
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Regular comment")

    def test_target_comment_wrong_object(self):
        """Test that target_comment from different object is ignored."""
        # Create comment on a different blog
        other_blog = BlogPost.objects.create(
            title="Other Blog",
            slug="other-blog",
            content="Other content",
            publish_on=timezone.now(),
            visible=True,
        )
        other_comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=other_blog.id,
            author=self.profile,
            body="Other blog comment",
        )

        # Create comment on our blog
        our_comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Our blog comment",
        )

        # Request our blog with target_comment from other blog
        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "target_comment": other_comment.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        # Our comment should appear, other blog's comment should not
        self.assertContains(response, "Our blog comment")
        self.assertNotContains(response, "Other blog comment")

    def test_target_comment_hidden(self):
        """Test that hidden target_comment is handled gracefully."""
        hidden_comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Hidden comment",
            hidden=True,
        )
        visible_comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Visible comment",
        )

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "target_comment": hidden_comment.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        # Visible comment should appear, hidden should not
        self.assertContains(response, "Visible comment")
        self.assertNotContains(response, "Hidden comment")

    def test_target_comment_deeply_nested(self):
        """Test that target_comment works for deeply nested comments (3+ levels)."""
        # Create a chain: root -> level1 -> level2 -> target
        root = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Root comment",
        )
        level1 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Level 1 comment",
            parent=root,
        )
        level2 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Level 2 comment",
            parent=level1,
        )
        target = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Target comment",
            parent=level2,
        )

        response = self.client.get(
            reverse("get_comments"),
            {
                "content_type_id": self.content_type.id,
                "object_id": self.blog.id,
                "target_comment": target.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        # All ancestors and target should appear in order
        self.assertIn("Root comment", content)
        self.assertIn("Level 1 comment", content)
        self.assertIn("Level 2 comment", content)
        self.assertIn("Target comment", content)
        # Verify order: root -> level1 -> level2 -> target
        root_pos = content.find("Root comment")
        level1_pos = content.find("Level 1 comment")
        level2_pos = content.find("Level 2 comment")
        target_pos = content.find("Target comment")
        self.assertLess(root_pos, level1_pos)
        self.assertLess(level1_pos, level2_pos)
        self.assertLess(level2_pos, target_pos)
