import json
from unittest.mock import patch, MagicMock

from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse

from judge.models import Profile, Language
from judge.widgets.direct_upload import generate_upload_token


class UserFileUploadConfirmSizeTest(TestCase):
    """Test that user_file_upload_confirm rejects oversized files."""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3C",
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
            username="size_test_user", password="password123"
        )
        self.user.is_superuser = True
        self.user.save()
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.client = Client()
        self.client.login(username="size_test_user", password="password123")

    def tearDown(self):
        cache.clear()

    @patch("judge.views.custom_file_upload.get_user_files")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_delete_file")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_confirm_rejects_oversized_file(
        self, mock_storage, mock_delete, mock_get_size, mock_get_files
    ):
        """Confirm should reject and delete a file that exceeds max_file_size."""
        # File is 100MB, max is 50MB for admin
        mock_get_size.return_value = 100 * 1024 * 1024

        response = self.client.post(
            reverse("user_file_upload_confirm"),
            data=json.dumps({"file_key": "user_uploads/size_test_user/big_file.zip"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("File size exceeds", data["error"])

        # Verify the oversized file was deleted
        mock_delete.assert_called_once()

    @patch("judge.views.custom_file_upload.get_user_files")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_confirm_accepts_valid_file(
        self, mock_storage, mock_get_size, mock_get_files
    ):
        """Confirm should accept a file within size limits."""
        mock_get_size.return_value = 1024  # 1KB
        mock_get_files.return_value = [{"name": "small.txt", "size": 1024}]
        mock_get_files.dirty = MagicMock()
        mock_storage.url = lambda x: f"/media/{x}"

        response = self.client.post(
            reverse("user_file_upload_confirm"),
            data=json.dumps({"file_key": "user_uploads/size_test_user/small.txt"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])

    @patch("judge.views.custom_file_upload.get_user_files")
    @patch("judge.views.custom_file_upload.storage_get_file_size")
    @patch("judge.views.custom_file_upload.storage_delete_file")
    @patch("judge.views.custom_file_upload.default_storage")
    def test_confirm_rejects_when_quota_exceeded(
        self, mock_storage, mock_delete, mock_get_size, mock_get_files
    ):
        """Confirm should reject and delete file if total storage exceeds quota."""
        # File itself is within per-file limit (10MB), but total exceeds 500MB admin quota
        mock_get_size.return_value = 10 * 1024 * 1024
        mock_get_files.return_value = [
            {"name": "existing.zip", "size": 495 * 1024 * 1024},
            {"name": "new_file.zip", "size": 10 * 1024 * 1024},
        ]
        mock_get_files.dirty = MagicMock()

        response = self.client.post(
            reverse("user_file_upload_confirm"),
            data=json.dumps({"file_key": "user_uploads/size_test_user/new_file.zip"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("Storage quota exceeded", data["error"])
        mock_delete.assert_called_once()

    def test_confirm_rejects_path_traversal(self):
        """Confirm should reject file_key outside user's storage path."""
        response = self.client.post(
            reverse("user_file_upload_confirm"),
            data=json.dumps({"file_key": "user_uploads/other_user/secret.txt"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("Invalid file path", data["error"])


class SaveToModelSizeTest(TestCase):
    """Test that save_to_model (DirectUploadWidget) rejects oversized files."""

    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3V",
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
            username="widget_test_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.client = Client()
        self.client.login(username="widget_test_user", password="password123")

    def tearDown(self):
        cache.clear()

    @patch("judge.views.direct_upload.default_storage")
    def test_save_rejects_oversized_file(self, mock_storage):
        """save_to_model should reject and delete file exceeding max_size from token."""
        max_size = 5 * 1024 * 1024  # 5MB
        actual_size = 50 * 1024 * 1024  # 50MB (spoofed)

        # Create a valid upload token with max_size
        token = generate_upload_token(
            profile_id=self.profile.id,
            model_name="judge.Profile",
            object_id=self.profile.pk,
            field_name="profile_image",
            max_size=max_size,
            upload_to="profile_images",
            prefix="test",
        )

        # Mock storage.size to return oversized file
        mock_storage.size.return_value = actual_size

        response = self.client.post(
            reverse("direct_upload_save"),
            data=json.dumps(
                {
                    "file_key": "profile_images/test_image.png",
                    "upload_token": token,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("File size exceeds", data["error"])

        # Verify the file was deleted from storage
        mock_storage.delete.assert_called_once_with("profile_images/test_image.png")

    @patch("judge.views.direct_upload.default_storage")
    def test_save_accepts_valid_file(self, mock_storage):
        """save_to_model should accept file within max_size."""
        max_size = 5 * 1024 * 1024  # 5MB
        actual_size = 1 * 1024 * 1024  # 1MB

        token = generate_upload_token(
            profile_id=self.profile.id,
            model_name="judge.Profile",
            object_id=self.profile.pk,
            field_name="profile_image",
            max_size=max_size,
            upload_to="profile_images",
            prefix="test",
        )

        mock_storage.size.return_value = actual_size

        response = self.client.post(
            reverse("direct_upload_save"),
            data=json.dumps(
                {
                    "file_key": "profile_images/test_image.png",
                    "upload_token": token,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
