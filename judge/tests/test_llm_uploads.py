import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from llm_service.llm_api import LLMService


class LLMServiceUploadTest(SimpleTestCase):
    def setUp(self):
        self.service = LLMService(api_key="test-key")

    @patch("llm_service.llm_api.fp.upload_file_sync")
    def test_upload_media_file_from_local_media_root(self, upload_file_sync):
        upload_file_sync.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "pagedown-uploads" / "image.png"
            image_path.parent.mkdir()
            image_path.write_bytes(b"image-bytes")

            with self.settings(MEDIA_ROOT=tmpdir):
                attachment = self.service._upload_local_file(
                    "/media/pagedown-uploads/image.png"
                )

        self.assertIs(attachment, upload_file_sync.return_value)
        upload_file_sync.assert_called_once_with(
            file=b"image-bytes",
            file_name="image.png",
            api_key="test-key",
        )

    @patch("llm_service.llm_api.default_storage")
    @patch("llm_service.llm_api.fp.upload_file_sync")
    def test_upload_media_file_from_default_storage_when_media_root_empty(
        self, upload_file_sync, default_storage
    ):
        upload_file_sync.return_value = MagicMock()
        default_storage.exists.return_value = True
        default_storage.open.return_value = BytesIO(b"s3-image-bytes")

        with self.settings(MEDIA_ROOT=""):
            attachment = self.service._upload_local_file(
                "/media/pagedown-uploads/image.png"
            )

        self.assertIs(attachment, upload_file_sync.return_value)
        default_storage.exists.assert_called_once_with("pagedown-uploads/image.png")
        default_storage.open.assert_called_once_with("pagedown-uploads/image.png", "rb")
        upload_file_sync.assert_called_once_with(
            file=b"s3-image-bytes",
            file_name="image.png",
            api_key="test-key",
        )

    @patch("llm_service.llm_api.default_storage")
    @patch("llm_service.llm_api.fp.upload_file_sync")
    def test_upload_site_cdn_media_url_uses_default_storage_first(
        self, upload_file_sync, default_storage
    ):
        upload_file_sync.return_value = MagicMock()
        default_storage.exists.return_value = True
        default_storage.open.return_value = BytesIO(b"cdn-media-bytes")

        with self.settings(MEDIA_ROOT=""), patch.object(
            self.service, "_get_site_domain", return_value="lqdoj.edu.vn"
        ), patch.object(self.service, "_upload_file_from_url") as upload_from_url:
            attachment = self.service.upload_file(
                "https://cdn.lqdoj.edu.vn/media/comments/picture.webp"
            )

        self.assertIs(attachment, upload_file_sync.return_value)
        default_storage.exists.assert_called_once_with("comments/picture.webp")
        default_storage.open.assert_called_once_with("comments/picture.webp", "rb")
        upload_from_url.assert_not_called()
        upload_file_sync.assert_called_once_with(
            file=b"cdn-media-bytes",
            file_name="picture.webp",
            api_key="test-key",
        )

    @patch("llm_service.llm_api.fp.upload_file_sync")
    def test_upload_problem_data_file_from_problem_data_root(self, upload_file_sync):
        upload_file_sync.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            statement_path = Path(tmpdir) / "abc" / "statement.pdf"
            statement_path.parent.mkdir()
            statement_path.write_bytes(b"pdf-bytes")

            with self.settings(DMOJ_PROBLEM_DATA_ROOT=tmpdir):
                attachment = self.service._upload_local_file(
                    "/problem/abc/data/statement.pdf"
                )

        self.assertIs(attachment, upload_file_sync.return_value)
        upload_file_sync.assert_called_once_with(
            file=b"pdf-bytes",
            file_name="statement.pdf",
            api_key="test-key",
        )

    @patch("llm_service.llm_api.default_storage")
    @patch("llm_service.llm_api.fp.upload_file_sync")
    def test_missing_default_storage_media_file_is_not_uploaded(
        self, upload_file_sync, default_storage
    ):
        default_storage.exists.return_value = False

        with self.settings(MEDIA_ROOT=""):
            attachment = self.service._upload_local_file("/media/missing.png")

        self.assertIsNone(attachment)
        default_storage.exists.assert_called_once_with("missing.png")
        default_storage.open.assert_not_called()
        upload_file_sync.assert_not_called()

    @patch("llm_service.llm_api.default_storage")
    @patch("llm_service.llm_api.fp.upload_file_sync")
    def test_unsafe_media_path_is_not_uploaded(self, upload_file_sync, default_storage):
        with self.settings(MEDIA_ROOT=""):
            attachment = self.service._upload_local_file("/media/%2e%2e/secret.png")

        self.assertIsNone(attachment)
        default_storage.exists.assert_not_called()
        default_storage.open.assert_not_called()
        upload_file_sync.assert_not_called()

    @patch("llm_service.llm_api.fp.upload_file_sync")
    def test_unsafe_problem_data_path_is_not_uploaded(self, upload_file_sync):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.settings(DMOJ_PROBLEM_DATA_ROOT=tmpdir):
                attachment = self.service._upload_local_file(
                    "/problem/../data/secret.pdf"
                )

        self.assertIsNone(attachment)
        upload_file_sync.assert_not_called()
