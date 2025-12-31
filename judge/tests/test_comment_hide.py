from django.test import TestCase, Client
from django.contrib.auth.models import User, Permission
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Organization,
    Profile,
    Language,
    BlogPost,
    Problem,
    ProblemGroup,
    ProblemType,
    Comment,
)


class CommentHidePermissionTestCase(TestCase):
    """Test cases for comment hiding permissions"""

    @classmethod
    def setUpTestData(cls):
        # Create default language required for Profile
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

        # Create problem group and type
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="Test Group",
            defaults={"full_name": "Test Problem Group"},
        )
        cls.problem_type, _ = ProblemType.objects.get_or_create(
            name="Test Type",
            defaults={"full_name": "Test Problem Type"},
        )

    def setUp(self):
        self.client = Client()

        # Create users
        self.admin_user = User.objects.create_user(
            username="admin_user", password="password123"
        )
        self.admin_profile, _ = Profile.objects.get_or_create(
            user=self.admin_user, defaults={"language": self.language}
        )
        # Give admin the change_comment permission
        permission = Permission.objects.get(codename="change_comment")
        self.admin_user.user_permissions.add(permission)

        self.blog_author = User.objects.create_user(
            username="blog_author", password="password123"
        )
        self.blog_author_profile, _ = Profile.objects.get_or_create(
            user=self.blog_author, defaults={"language": self.language}
        )

        self.problem_author = User.objects.create_user(
            username="problem_author", password="password123"
        )
        self.problem_author_profile, _ = Profile.objects.get_or_create(
            user=self.problem_author, defaults={"language": self.language}
        )

        self.regular_user = User.objects.create_user(
            username="regular_user", password="password123"
        )
        self.regular_profile, _ = Profile.objects.get_or_create(
            user=self.regular_user, defaults={"language": self.language}
        )

        self.commenter = User.objects.create_user(
            username="commenter", password="password123"
        )
        self.commenter_profile, _ = Profile.objects.get_or_create(
            user=self.commenter, defaults={"language": self.language}
        )

        # Create a blog post
        self.blog_post = BlogPost.objects.create(
            title="Test Blog Post",
            slug="test-blog-post",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        self.blog_post.authors.add(self.blog_author_profile)

        # Create a problem
        self.problem = Problem.objects.create(
            code="testprob",
            name="Test Problem",
            description="Test description",
            group=self.problem_group,
            points=10,
            time_limit=1.0,
            memory_limit=256 * 1024,
        )
        self.problem.types.add(self.problem_type)
        self.problem.authors.add(self.problem_author_profile)

        # Create comments
        blog_content_type = ContentType.objects.get_for_model(BlogPost)
        problem_content_type = ContentType.objects.get_for_model(Problem)

        self.blog_comment = Comment.objects.create(
            content_type=blog_content_type,
            object_id=self.blog_post.id,
            author=self.commenter_profile,
            body="Test blog comment",
            hidden=False,
        )

        self.problem_comment = Comment.objects.create(
            content_type=problem_content_type,
            object_id=self.problem.id,
            author=self.commenter_profile,
            body="Test problem comment",
            hidden=False,
        )

    def test_admin_can_hide_any_comment(self):
        """Test that users with change_comment permission can hide any comment"""
        self.client.login(username="admin_user", password="password123")

        response = self.client.post(
            reverse("comment_hide"),
            {"id": self.blog_comment.id},
        )
        self.assertEqual(response.status_code, 200)

        self.blog_comment.refresh_from_db()
        self.assertTrue(self.blog_comment.hidden)

    def test_blog_author_can_hide_comment_on_own_blog(self):
        """Test that blog authors can hide comments on their own posts"""
        self.client.login(username="blog_author", password="password123")

        response = self.client.post(
            reverse("comment_hide"),
            {"id": self.blog_comment.id},
        )
        self.assertEqual(response.status_code, 200)

        self.blog_comment.refresh_from_db()
        self.assertTrue(self.blog_comment.hidden)

    def test_problem_author_can_hide_comment_on_own_problem(self):
        """Test that problem authors can hide comments on their own problems"""
        self.client.login(username="problem_author", password="password123")

        response = self.client.post(
            reverse("comment_hide"),
            {"id": self.problem_comment.id},
        )
        self.assertEqual(response.status_code, 200)

        self.problem_comment.refresh_from_db()
        self.assertTrue(self.problem_comment.hidden)

    def test_regular_user_cannot_hide_comment(self):
        """Test that regular users cannot hide comments"""
        self.client.login(username="regular_user", password="password123")

        response = self.client.post(
            reverse("comment_hide"),
            {"id": self.blog_comment.id},
        )
        self.assertEqual(response.status_code, 403)

        self.blog_comment.refresh_from_db()
        self.assertFalse(self.blog_comment.hidden)

    def test_blog_author_cannot_hide_comment_on_others_blog(self):
        """Test that blog authors cannot hide comments on others' blogs"""
        # Create another blog post by a different author
        other_blog = BlogPost.objects.create(
            title="Other Blog Post",
            slug="other-blog-post",
            content="Other content",
            publish_on=timezone.now(),
            visible=True,
        )
        other_blog.authors.add(self.regular_profile)

        blog_content_type = ContentType.objects.get_for_model(BlogPost)
        other_comment = Comment.objects.create(
            content_type=blog_content_type,
            object_id=other_blog.id,
            author=self.commenter_profile,
            body="Comment on other blog",
            hidden=False,
        )

        self.client.login(username="blog_author", password="password123")

        response = self.client.post(
            reverse("comment_hide"),
            {"id": other_comment.id},
        )
        self.assertEqual(response.status_code, 403)

        other_comment.refresh_from_db()
        self.assertFalse(other_comment.hidden)

    def test_unauthenticated_user_cannot_hide_comment(self):
        """Test that unauthenticated users cannot hide comments"""
        response = self.client.post(
            reverse("comment_hide"),
            {"id": self.blog_comment.id},
        )
        # Should redirect to login or return 403
        self.assertIn(response.status_code, [302, 403])

        self.blog_comment.refresh_from_db()
        self.assertFalse(self.blog_comment.hidden)

    def test_hiding_comment_hides_replies(self):
        """Test that hiding a comment also hides its replies"""
        blog_content_type = ContentType.objects.get_for_model(BlogPost)

        # Create a reply to the blog comment
        reply = Comment.objects.create(
            content_type=blog_content_type,
            object_id=self.blog_post.id,
            author=self.regular_profile,
            body="Reply to blog comment",
            parent=self.blog_comment,
            hidden=False,
        )

        self.client.login(username="blog_author", password="password123")

        response = self.client.post(
            reverse("comment_hide"),
            {"id": self.blog_comment.id},
        )
        self.assertEqual(response.status_code, 200)

        self.blog_comment.refresh_from_db()
        reply.refresh_from_db()

        self.assertTrue(self.blog_comment.hidden)
        self.assertTrue(reply.hidden)
