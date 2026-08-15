import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from judge.models import Language, Profile
from judge.widgets.direct_upload import generate_upload_token


class ThemeSettingsSampleBackgroundTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3T",
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
            username="theme_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.client = Client()
        self.client.login(username="theme_user", password="password123")

    def tearDown(self):
        cache.clear()

    @patch("judge.views.theme.Profile.dirty_cache")
    @patch("judge.views.theme.default_storage.open")
    @patch("judge.views.theme.default_storage.delete")
    @patch("judge.views.theme.storage_file_exists", return_value=True)
    def test_select_sample_stores_shared_path_without_copying_file(
        self, mock_exists, mock_delete, mock_open, mock_dirty_cache
    ):
        self.profile.background_image = "background_images/old.jpg"
        self.profile.save(update_fields=["background_image"])
        mock_dirty_cache.reset_mock()

        response = self.client.post(
            reverse("theme_settings"),
            {"action": "select_sample", "sample_filename": "summer.jpg"},
        )

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(
            self.profile.background_image.name, "sample_backgrounds/summer.jpg"
        )
        mock_exists.assert_called_once()
        mock_open.assert_not_called()
        mock_delete.assert_called_once_with("background_images/old.jpg")
        mock_dirty_cache.assert_called_once_with(self.profile.pk)

    @patch("judge.views.theme.default_storage.delete")
    @patch("judge.views.theme.storage_file_exists", return_value=True)
    def test_select_sample_does_not_delete_previous_shared_sample(
        self, mock_exists, mock_delete
    ):
        self.profile.background_image = "sample_backgrounds/old.jpg"
        self.profile.save(update_fields=["background_image"])

        response = self.client.post(
            reverse("theme_settings"),
            {"action": "select_sample", "sample_filename": "new.jpg"},
        )

        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(
            self.profile.background_image.name, "sample_backgrounds/new.jpg"
        )
        mock_exists.assert_called_once()
        mock_delete.assert_not_called()

    @patch("judge.views.theme.storage_file_exists")
    def test_select_sample_rejects_path_traversal(self, mock_exists):
        response = self.client.post(
            reverse("theme_settings"),
            {"action": "select_sample", "sample_filename": "../secret.jpg"},
        )

        self.assertEqual(response.status_code, 403)
        mock_exists.assert_not_called()

    @patch("judge.views.theme.storage_delete_file")
    def test_admin_cannot_delete_sample_background_while_in_use(self, mock_delete):
        self.user.is_superuser = True
        self.user.save(update_fields=["is_superuser"])
        self.profile.background_image = "sample_backgrounds/in-use.jpg"
        self.profile.save(update_fields=["background_image"])

        response = self.client.post(
            reverse("delete_sample_background"), {"filename": "in-use.jpg"}
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])
        mock_delete.assert_not_called()


class ThemeDirectUploadSampleBackgroundTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3D",
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
            username="theme_upload_user", password="password123"
        )
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.client = Client()
        self.client.login(username="theme_upload_user", password="password123")

    def tearDown(self):
        cache.clear()

    def make_background_upload_token(self):
        return generate_upload_token(
            profile_id=self.profile.id,
            model_name="judge.Profile",
            object_id=self.profile.pk,
            field_name="background_image",
            max_size=None,
            upload_to="background_images",
            prefix="bg_user",
        )

    @patch("judge.views.direct_upload.default_storage")
    def test_save_to_model_does_not_delete_previous_shared_sample(self, mock_storage):
        self.profile.background_image = "sample_backgrounds/current.jpg"
        self.profile.save(update_fields=["background_image"])

        response = self.client.post(
            reverse("direct_upload_save"),
            data=json.dumps(
                {
                    "file_key": "background_images/new.jpg",
                    "upload_token": self.make_background_upload_token(),
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(
            self.profile.background_image.name, "background_images/new.jpg"
        )
        mock_storage.delete.assert_not_called()

    @patch("judge.views.direct_upload.default_storage")
    def test_delete_file_clears_shared_sample_without_deleting_storage(
        self, mock_storage
    ):
        self.profile.background_image = "sample_backgrounds/current.jpg"
        self.profile.save(update_fields=["background_image"])

        response = self.client.post(
            reverse("direct_upload_delete"),
            data=json.dumps({"upload_token": self.make_background_upload_token()}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.background_image)
        mock_storage.delete.assert_not_called()
