from unittest.mock import patch, MagicMock
from datetime import datetime

from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from django.core.cache import cache

from judge.models import Profile, Language
from judge.views.custom_file_upload import (
    get_user_files,
    get_user_storage_usage,
)


class GetUserFilesCacheTestCase(TestCase):
    """Test cases for get_user_files caching behavior"""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3U",
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
            username="upload_test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    def _create_local_storage_mock(self):
        """Create a mock for local storage (no bucket attribute)"""
        mock_storage = MagicMock()
        # Delete bucket attribute to simulate local storage
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        return mock_storage

    @patch("judge.views.custom_file_upload.storage_listdir")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_get_modified_time")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_get_user_files_caches_result(
        self, mock_storage, mock_modified, mock_size, mock_listdir
    ):
        """get_user_files should cache results after first call"""
        # Configure mock for local storage
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        mock_listdir.return_value = ([], ["test1.txt", "test2.png"])
        mock_size.side_effect = [1024, 2048]
        mock_modified.side_effect = [datetime.now(), datetime.now()]

        # First call - should hit storage
        result1 = get_user_files("upload_test_user")
        self.assertEqual(len(result1), 2)
        self.assertEqual(mock_listdir.call_count, 1)

        # Second call - should use cache
        result2 = get_user_files("upload_test_user")
        self.assertEqual(len(result2), 2)
        self.assertEqual(mock_listdir.call_count, 1)  # Still 1, not 2

    @patch("judge.views.custom_file_upload.storage_listdir")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_get_modified_time")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_get_user_files_dirty_invalidates_cache(
        self, mock_storage, mock_modified, mock_size, mock_listdir
    ):
        """get_user_files.dirty() should invalidate the cache"""
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        mock_listdir.return_value = ([], ["test1.txt"])
        mock_size.return_value = 1024
        mock_modified.return_value = datetime.now()

        # First call
        result1 = get_user_files("upload_test_user")
        self.assertEqual(len(result1), 1)
        self.assertEqual(mock_listdir.call_count, 1)

        # Dirty the cache
        get_user_files.dirty("upload_test_user")

        # Update mock to return different data
        mock_listdir.return_value = ([], ["test1.txt", "test2.txt"])
        mock_size.side_effect = [1024, 2048]
        mock_modified.side_effect = [datetime.now(), datetime.now()]

        # Call again - should hit storage
        result2 = get_user_files("upload_test_user")
        self.assertEqual(len(result2), 2)
        self.assertEqual(mock_listdir.call_count, 2)

    @patch("judge.views.custom_file_upload.storage_listdir")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_get_modified_time")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_different_users_have_separate_caches(
        self, mock_storage, mock_modified, mock_size, mock_listdir
    ):
        """Each user should have their own cache entry"""
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        mock_listdir.return_value = ([], ["file.txt"])
        mock_size.return_value = 1024
        mock_modified.return_value = datetime.now()

        # Call for user1
        get_user_files("user1")
        self.assertEqual(mock_listdir.call_count, 1)

        # Call for user2 - should hit storage (different cache key)
        get_user_files("user2")
        self.assertEqual(mock_listdir.call_count, 2)

        # Call for user1 again - should use cache
        get_user_files("user1")
        self.assertEqual(mock_listdir.call_count, 2)

    @patch("judge.views.custom_file_upload.storage_listdir")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_get_modified_time")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_dirty_only_affects_specified_user(
        self, mock_storage, mock_modified, mock_size, mock_listdir
    ):
        """Dirtying one user's cache should not affect another user"""
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        mock_listdir.return_value = ([], ["file.txt"])
        mock_size.return_value = 1024
        mock_modified.return_value = datetime.now()

        # Populate cache for both users
        get_user_files("user1")
        get_user_files("user2")
        self.assertEqual(mock_listdir.call_count, 2)

        # Dirty only user1's cache
        get_user_files.dirty("user1")

        # user1 should hit storage again
        get_user_files("user1")
        self.assertEqual(mock_listdir.call_count, 3)

        # user2 should still use cache
        get_user_files("user2")
        self.assertEqual(mock_listdir.call_count, 3)


class GetUserStorageUsageTestCase(TestCase):
    """Test cases for get_user_storage_usage using cached get_user_files"""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3S",
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
            username="storage_test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    @patch("judge.views.custom_file_upload.storage_listdir")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_get_modified_time")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_get_user_storage_usage_uses_cached_files(
        self, mock_storage, mock_modified, mock_size, mock_listdir
    ):
        """get_user_storage_usage should use cached get_user_files"""
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        mock_listdir.return_value = ([], ["test1.txt", "test2.txt"])
        mock_size.side_effect = [1000, 2000]
        mock_modified.side_effect = [datetime.now(), datetime.now()]

        # First call to get_user_storage_usage
        usage1 = get_user_storage_usage("storage_test_user", self.user)
        self.assertEqual(usage1["used"], 3000)
        self.assertEqual(mock_listdir.call_count, 1)

        # Second call - should use cached files
        usage2 = get_user_storage_usage("storage_test_user", self.user)
        self.assertEqual(usage2["used"], 3000)
        self.assertEqual(mock_listdir.call_count, 1)  # Still 1

    @patch("judge.views.custom_file_upload.storage_listdir")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_get_modified_time")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_storage_usage_updates_after_cache_dirty(
        self, mock_storage, mock_modified, mock_size, mock_listdir
    ):
        """Storage usage should reflect new files after cache invalidation"""
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        mock_listdir.return_value = ([], ["test1.txt"])
        mock_size.return_value = 1000
        mock_modified.return_value = datetime.now()

        # Initial usage
        usage1 = get_user_storage_usage("storage_test_user", self.user)
        self.assertEqual(usage1["used"], 1000)

        # Simulate file addition by dirtying cache and changing mock
        get_user_files.dirty("storage_test_user")
        mock_listdir.return_value = ([], ["test1.txt", "test2.txt"])
        mock_size.side_effect = [1000, 5000]
        mock_modified.side_effect = [datetime.now(), datetime.now()]

        # Usage should now reflect new file
        usage2 = get_user_storage_usage("storage_test_user", self.user)
        self.assertEqual(usage2["used"], 6000)


class CacheInvalidationOnFileOperationsTestCase(TestCase):
    """Test that file operations properly invalidate cache"""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3O",
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
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="ops_test_user", password="password123"
        )
        self.user.is_superuser = True
        self.user.save()
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )

    def tearDown(self):
        cache.clear()

    @patch("judge.views.custom_file_upload.storage_listdir")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_get_modified_time")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_empty_file_list_is_cached(
        self, mock_storage, mock_modified, mock_size, mock_listdir
    ):
        """Empty file list should be cached correctly"""
        del mock_storage.bucket
        mock_storage.url = lambda x: f"/media/{x}"
        mock_listdir.return_value = ([], [])

        # First call
        result1 = get_user_files("emptyuser")
        self.assertEqual(result1, [])
        self.assertEqual(mock_listdir.call_count, 1)

        # Second call should use cache
        result2 = get_user_files("emptyuser")
        self.assertEqual(result2, [])
        self.assertEqual(mock_listdir.call_count, 1)
