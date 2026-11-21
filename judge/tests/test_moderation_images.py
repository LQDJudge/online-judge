from io import BytesIO
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from judge.moderation_images import (
    ModerationImageError,
    _upload_public_image,
    read_uploaded_moderation_image,
)


class ModerationImageSafetyTests(SimpleTestCase):
    @override_settings(MEDIA_URL="https://cdn.example.com/media/")
    @patch("judge.moderation_images.default_storage.size", return_value=11)
    @patch("judge.moderation_images.default_storage.open")
    def test_site_upload_is_read_from_storage_and_sent_to_poe(self, open_file, size):
        open_file.return_value = BytesIO(b"image-bytes")
        service = Mock()
        _upload_public_image(
            "https://cdn.example.com/media/pagedown-uploads/a.png", service
        )
        open_file.assert_called_once_with("pagedown-uploads/a.png", "rb")
        self.assertEqual(service._upload_bytes_to_poe.call_args.args[0], b"image-bytes")

    @override_settings(MEDIA_URL="https://cdn.example.com/media/")
    @patch("judge.moderation_images.default_storage.open")
    def test_storage_access_rejects_other_hosts_private_paths_and_traversal(
        self, open_file
    ):
        for url in (
            "https://evil.example/media/pagedown-uploads/a.png",
            "https://cdn.example.com/media/private/a.png",
            "https://cdn.example.com/media/pagedown-uploads/%2e%2e/a.png",
            "https://cdn.example.com/media/pagedown-uploads/%252e%252e/a.png",
        ):
            self.assertIsNone(read_uploaded_moderation_image(url))
        open_file.assert_not_called()

    @override_settings(MEDIA_URL="https://cdn.example.com/media/")
    @patch(
        "judge.moderation_images.default_storage.size", return_value=5 * 1024 * 1024 + 1
    )
    @patch("judge.moderation_images.default_storage.open")
    def test_site_upload_size_is_bounded(self, open_file, size):
        with self.assertRaises(ModerationImageError):
            read_uploaded_moderation_image(
                "https://cdn.example.com/media/pagedown-uploads/a.png"
            )
        open_file.assert_not_called()

    @override_settings(MEDIA_URL="https://cdn.example.com/media/")
    @patch("judge.moderation_images.default_storage.size", return_value=1)
    @patch("judge.moderation_images.default_storage.open")
    def test_site_upload_checks_actual_bytes_as_well_as_size(self, open_file, size):
        open_file.return_value = BytesIO(b"x" * (5 * 1024 * 1024 + 1))
        with self.assertRaises(ModerationImageError):
            read_uploaded_moderation_image(
                "https://cdn.example.com/media/pagedown-uploads/a.png"
            )

    @patch("judge.moderation_images.Site.objects.get_current")
    @patch("judge.moderation_images.socket.getaddrinfo")
    @patch("judge.moderation_images.urllib3.HTTPSConnectionPool")
    def test_private_or_mixed_dns_never_connects(self, pool, dns, site):
        site.return_value.domain = "example.com"
        dns.return_value = [
            (None, None, None, None, ("8.8.8.8", 443)),
            (None, None, None, None, ("127.0.0.1", 443)),
        ]
        with self.assertRaises(ModerationImageError):
            _upload_public_image("https://example.com/image.png", Mock())
        pool.assert_not_called()

    @patch("judge.moderation_images.Site.objects.get_current")
    @patch("judge.moderation_images.socket.getaddrinfo")
    @patch("judge.moderation_images.urllib3.HTTPSConnectionPool")
    def test_download_is_pinned_and_redirects_are_not_followed(self, pool, dns, site):
        site.return_value.domain = "example.com"
        dns.return_value = [(None, None, None, None, ("8.8.8.8", 443))]
        response = Mock(status=302, headers={"Content-Type": "image/png"})
        pool.return_value.request.return_value = response
        with self.assertRaises(ModerationImageError):
            _upload_public_image("https://example.com/image.png", Mock())
        self.assertEqual(pool.call_args.args[0], "8.8.8.8")
        self.assertEqual(pool.call_args.kwargs["assert_hostname"], "example.com")
        self.assertFalse(pool.return_value.request.call_args.kwargs["redirect"])

    @patch("judge.moderation_images.Site.objects.get_current")
    @patch("judge.moderation_images.socket.getaddrinfo")
    @patch("judge.moderation_images.urllib3.HTTPSConnectionPool")
    def test_oversized_image_is_rejected(self, pool, dns, site):
        site.return_value.domain = "example.com"
        dns.return_value = [(None, None, None, None, ("8.8.8.8", 443))]
        response = Mock(status=200, headers={"Content-Type": "image/png"})
        response.read.return_value = b"x" * (5 * 1024 * 1024 + 1)
        pool.return_value.request.return_value = response
        with self.assertRaises(ModerationImageError):
            _upload_public_image("https://example.com/image.png", Mock())
