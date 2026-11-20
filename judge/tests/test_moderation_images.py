from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from judge.moderation_images import ModerationImageError, _upload_public_image


class ModerationImageSafetyTests(SimpleTestCase):
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
