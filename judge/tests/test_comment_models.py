from django.core.cache import cache
from django.test import TestCase
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from judge.models import (
    Profile,
    Language,
    BlogPost,
    Problem,
    ProblemGroup,
    Comment,
)
from judge.models.comment import (
    get_visible_comment_count,
    get_visible_top_level_comment_count,
    get_visible_reply_count,
    get_top_level_comment_ids,
    get_reply_ids,
)


class CommentModelTestCase(TestCase):
    """Test cases for Comment model and caching."""

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
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="test",
            defaults={"full_name": "Test Group"},
        )

    def setUp(self):
        cache.clear()

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

    def tearDown(self):
        cache.clear()

    def test_comment_creation(self):
        """Test basic comment creation."""
        content_type = ContentType.objects.get_for_model(BlogPost)
        comment = Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test comment",
        )

        self.assertEqual(comment.body, "Test comment")
        self.assertEqual(comment.author, self.profile)
        self.assertFalse(comment.hidden)
        self.assertEqual(comment.score, 0)

    def test_comment_count_cache(self):
        """Test that comment count is properly cached and updated."""
        content_type = ContentType.objects.get_for_model(BlogPost)

        # Initial count should be 0
        count = get_visible_comment_count(content_type.id, self.blog.id)
        self.assertEqual(count, 0)

        # Create a comment - save() should dirty the cache
        Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test comment",
        )

        # Count should update after cache is dirtied (via save override)
        count = get_visible_comment_count(content_type.id, self.blog.id)
        self.assertEqual(count, 1)

    def test_hidden_comments_not_counted(self):
        """Test that hidden comments are not included in count."""
        content_type = ContentType.objects.get_for_model(BlogPost)

        Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Visible comment",
            hidden=False,
        )
        Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Hidden comment",
            hidden=True,
        )

        count = get_visible_comment_count(content_type.id, self.blog.id)
        self.assertEqual(count, 1)

    def test_top_level_count_excludes_replies(self):
        """Test that top-level count excludes reply comments."""
        content_type = ContentType.objects.get_for_model(BlogPost)

        parent = Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )
        Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )

        top_level_count = get_visible_top_level_comment_count(
            content_type.id, self.blog.id
        )
        total_count = get_visible_comment_count(content_type.id, self.blog.id)

        self.assertEqual(top_level_count, 1)
        self.assertEqual(total_count, 2)

    def test_cache_invalidated_on_delete(self):
        """Test that cache is invalidated when a comment is deleted."""
        content_type = ContentType.objects.get_for_model(BlogPost)

        comment = Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test comment",
        )

        # Verify count is 1
        count = get_visible_comment_count(content_type.id, self.blog.id)
        self.assertEqual(count, 1)

        # Delete comment - delete() should dirty the cache
        comment.delete()

        # Count should be 0 after cache is dirtied
        count = get_visible_comment_count(content_type.id, self.blog.id)
        self.assertEqual(count, 0)

    def test_comment_get_absolute_url(self):
        """Test comment's get_absolute_url method."""
        content_type = ContentType.objects.get_for_model(BlogPost)

        comment = Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test comment",
        )

        url = comment.get_absolute_url()
        self.assertIn(f"target_comment={comment.id}", url)
        self.assertIn(f"#comment-{comment.id}", url)

    def test_comment_page_title_for_blog(self):
        """Test that page_title returns blog title for blog comments."""
        content_type = ContentType.objects.get_for_model(BlogPost)

        comment = Comment.objects.create(
            content_type=content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test comment",
        )

        self.assertEqual(comment.page_title, self.blog.title)

    def test_comment_page_title_for_problem(self):
        """Test that page_title returns problem name for problem comments."""
        problem = Problem.objects.create(
            code="testprob",
            name="Test Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
        )
        content_type = ContentType.objects.get_for_model(Problem)

        comment = Comment.objects.create(
            content_type=content_type,
            object_id=problem.id,
            author=self.profile,
            body="Test comment",
        )

        self.assertEqual(comment.page_title, problem.name)


class CommentCacheableModelTestCase(TestCase):
    """Test cases for Comment as a CacheableModel."""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3CM",
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

        self.user = User.objects.create_user(
            username="test_cacheable_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.blog = BlogPost.objects.create(
            title="Test Blog Cacheable",
            slug="test-blog-cacheable",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        self.content_type = ContentType.objects.get_for_model(BlogPost)

    def tearDown(self):
        cache.clear()

    def test_get_cached_instances_returns_valid_comments(self):
        """get_cached_instances should return Comment instances for valid IDs."""
        comment1 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Comment 1",
        )
        comment2 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Comment 2",
        )

        instances = Comment.get_cached_instances(comment1.id, comment2.id)

        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[0].id, comment1.id)
        self.assertEqual(instances[1].id, comment2.id)

    def test_get_cached_instances_filters_deleted(self):
        """get_cached_instances should filter out deleted comments."""
        comment1 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Comment to keep",
        )
        comment2 = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Comment to delete",
        )

        comment1_id = comment1.id
        comment2_id = comment2.id

        # Delete comment2
        comment2.delete()

        # get_cached_instances should only return comment1
        instances = Comment.get_cached_instances(comment1_id, comment2_id)

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].id, comment1_id)

    def test_cached_instance_getter_methods(self):
        """Test that getter methods work on cached instances."""
        comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Test body content",
            score=5,
        )

        # Clear cache and get cached instance
        Comment.dirty_cache(comment.id)

        # Create instance from just ID (simulates cached instance)
        cached_comment = Comment(id=comment.id)

        # Getter methods should return correct values
        self.assertEqual(cached_comment.get_body(), "Test body content")
        self.assertEqual(cached_comment.get_score(), 5)
        self.assertEqual(cached_comment.get_author_id(), self.profile.id)
        self.assertFalse(cached_comment.get_hidden())
        self.assertEqual(cached_comment.get_content_type_id(), self.content_type.id)
        self.assertEqual(cached_comment.get_object_id(), self.blog.id)
        self.assertIsNone(cached_comment.get_parent_id())
        self.assertEqual(cached_comment.get_revision_count(), 1)
        self.assertIsNotNone(cached_comment.get_time())

    def test_dirty_cache_invalidates_values(self):
        """dirty_cache should invalidate cached values."""
        comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Original body",
            score=0,
        )

        # Populate cache
        cached = Comment(id=comment.id)
        self.assertEqual(cached.get_body(), "Original body")

        # Update directly in DB (bypassing model.save)
        Comment.objects.filter(id=comment.id).update(body="Updated body")

        # Cache still has old value
        cached2 = Comment(id=comment.id)
        self.assertEqual(cached2.get_body(), "Original body")

        # Dirty the cache
        Comment.dirty_cache(comment.id)

        # Now returns new value
        cached3 = Comment(id=comment.id)
        self.assertEqual(cached3.get_body(), "Updated body")

    def test_save_automatically_dirties_cache(self):
        """Saving a comment should automatically invalidate its cache."""
        comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Original body",
        )

        # Populate cache
        self.assertEqual(Comment(id=comment.id).get_body(), "Original body")

        # Update via save() - should auto-dirty
        comment.body = "Saved body"
        comment.save()

        # Should return new value
        self.assertEqual(Comment(id=comment.id).get_body(), "Saved body")


class CommentCacheInvalidationTestCase(TestCase):
    """Test cases for cache invalidation when comments are added/removed."""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3CI",
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

        self.user = User.objects.create_user(
            username="test_cache_inv_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

        self.blog = BlogPost.objects.create(
            title="Test Blog Cache Inv",
            slug="test-blog-cache-inv",
            content="Test content",
            publish_on=timezone.now(),
            visible=True,
        )
        self.content_type = ContentType.objects.get_for_model(BlogPost)

    def tearDown(self):
        cache.clear()

    def test_reply_count_updated_when_reply_added(self):
        """Reply count cache should update when a reply is added."""
        parent = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )

        # Initial reply count should be 0
        count = get_visible_reply_count(parent.id)
        self.assertEqual(count, 0)

        # Add a reply
        Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )

        # Reply count should now be 1
        count = get_visible_reply_count(parent.id)
        self.assertEqual(count, 1)

    def test_reply_count_updated_when_reply_deleted(self):
        """Reply count cache should update when a reply is deleted."""
        parent = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )
        reply = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )

        # Verify reply count is 1
        count = get_visible_reply_count(parent.id)
        self.assertEqual(count, 1)

        # Delete the reply
        reply.delete()

        # Reply count should now be 0
        count = get_visible_reply_count(parent.id)
        self.assertEqual(count, 0)

    def test_top_level_comment_ids_updated_on_create(self):
        """Top-level comment IDs cache should update when a comment is added."""
        # Initial IDs should be empty
        ids = get_top_level_comment_ids(
            self.content_type.id, self.blog.id, "time", "desc"
        )
        self.assertEqual(len(ids), 0)

        # Add a top-level comment
        comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Top level comment",
        )

        # IDs should now include the new comment
        ids = get_top_level_comment_ids(
            self.content_type.id, self.blog.id, "time", "desc"
        )
        self.assertEqual(len(ids), 1)
        self.assertEqual(ids[0], comment.id)

    def test_top_level_comment_ids_updated_on_delete(self):
        """Top-level comment IDs cache should update when a comment is deleted."""
        comment = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Top level comment",
        )

        # Verify IDs include the comment
        ids = get_top_level_comment_ids(
            self.content_type.id, self.blog.id, "time", "desc"
        )
        self.assertEqual(len(ids), 1)

        # Delete the comment
        comment.delete()

        # IDs should now be empty
        ids = get_top_level_comment_ids(
            self.content_type.id, self.blog.id, "time", "desc"
        )
        self.assertEqual(len(ids), 0)

    def test_reply_ids_updated_on_create(self):
        """Reply IDs cache should update when a reply is added."""
        parent = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )

        # Initial reply IDs should be empty
        ids = get_reply_ids(parent.id, "asc")
        self.assertEqual(len(ids), 0)

        # Add a reply
        reply = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )

        # Reply IDs should now include the new reply
        ids = get_reply_ids(parent.id, "asc")
        self.assertEqual(len(ids), 1)
        self.assertEqual(ids[0], reply.id)

    def test_reply_ids_updated_on_delete(self):
        """Reply IDs cache should update when a reply is deleted."""
        parent = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )
        reply = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )

        # Verify reply IDs include the reply
        ids = get_reply_ids(parent.id, "asc")
        self.assertEqual(len(ids), 1)

        # Delete the reply
        reply.delete()

        # Reply IDs should now be empty
        ids = get_reply_ids(parent.id, "asc")
        self.assertEqual(len(ids), 0)

    def test_get_reply_count_method_uses_cache(self):
        """Comment.get_reply_count() should use the cached function."""
        parent = Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Parent comment",
        )

        # Initial count should be 0
        self.assertEqual(parent.get_reply_count(), 0)

        # Add a reply
        Comment.objects.create(
            content_type=self.content_type,
            object_id=self.blog.id,
            author=self.profile,
            body="Reply comment",
            parent=parent,
        )

        # get_reply_count should return 1
        self.assertEqual(parent.get_reply_count(), 1)
