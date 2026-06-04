from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings

from judge.markdown import markdown, _iframe_host_allowed
from judge.middleware import ContentSecurityPolicyMiddleware

ALLOWED = [
    "www.youtube.com",
    "youtube.com",
    "docs.google.com",
]


@override_settings(IFRAME_ALLOWED_HOSTS=ALLOWED)
class IframeHostAllowedTest(SimpleTestCase):
    def test_allowlisted_hosts_pass(self):
        self.assertTrue(_iframe_host_allowed("https://www.youtube.com/embed/x"))
        self.assertTrue(_iframe_host_allowed("https://youtube.com/embed/x"))
        self.assertTrue(_iframe_host_allowed("https://docs.google.com/document/d/x"))

    def test_unlisted_host_blocked(self):
        self.assertFalse(_iframe_host_allowed("https://evil.example/login"))

    def test_empty_or_missing_src_blocked(self):
        self.assertFalse(_iframe_host_allowed(""))
        self.assertFalse(_iframe_host_allowed(None))

    def test_subdomain_suffix_trick_blocked(self):
        # An attacker domain merely ending with the brand must not pass.
        self.assertFalse(_iframe_host_allowed("https://youtube.com.evil.example/x"))

    def test_userinfo_trick_blocked(self):
        # The real host is evil.example, not youtube.com.
        self.assertFalse(_iframe_host_allowed("https://youtube.com@evil.example/x"))

    def test_case_insensitive(self):
        self.assertTrue(_iframe_host_allowed("https://WWW.YouTube.CoM/embed/x"))

    def test_port_stripped(self):
        self.assertTrue(_iframe_host_allowed("https://www.youtube.com:443/embed/x"))


@override_settings(IFRAME_ALLOWED_HOSTS=ALLOWED)
class MarkdownIframeSanitizeTest(SimpleTestCase):
    def test_allowed_iframe_kept(self):
        html = markdown('<iframe src="https://www.youtube.com/embed/abc"></iframe>')
        self.assertIn("<iframe", html)
        self.assertIn("www.youtube.com/embed/abc", html)

    def test_disallowed_iframe_becomes_link(self):
        html = markdown('<iframe src="https://evil.example/fake-login"></iframe>')
        self.assertNotIn("<iframe", html)
        # The URL is preserved as a plain, non-embedding link.
        self.assertIn("https://evil.example/fake-login", html)
        self.assertIn("<a", html)

    def test_subdomain_trick_iframe_blocked(self):
        html = markdown('<iframe src="https://youtube.com.evil.example/x"></iframe>')
        self.assertNotIn("<iframe", html)

    def test_youtube_link_autoembed_kept(self):
        # The YouTube markdown extension turns a bare link into an embed iframe;
        # that iframe targets www.youtube.com and must survive sanitizing.
        html = markdown("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertIn("<iframe", html)
        self.assertIn("youtube.com/embed/", html)


@override_settings(
    IFRAME_ALLOWED_HOSTS=["www.youtube.com", "docs.google.com"],
    MEDIA_URL="/media/",
)
class ContentSecurityPolicyMiddlewareTest(SimpleTestCase):
    def _make_response(self):
        middleware = ContentSecurityPolicyMiddleware(lambda request: HttpResponse("ok"))
        return middleware(request=None)

    def test_frame_src_directive_set(self):
        response = self._make_response()
        csp = response["Content-Security-Policy"]
        self.assertIn("frame-src", csp)
        self.assertIn("'self'", csp)
        self.assertIn("https://www.youtube.com", csp)
        self.assertIn("https://docs.google.com", csp)

    def test_no_default_src(self):
        # Only frame-src is constrained; the rest of the page is untouched.
        csp = self._make_response()["Content-Security-Policy"]
        self.assertNotIn("default-src", csp)

    @override_settings(MEDIA_URL="https://cdn.example.com/media/")
    def test_remote_media_origin_allowed_for_pdf_embeds(self):
        csp = self._make_response()["Content-Security-Policy"]
        self.assertIn("https://cdn.example.com", csp)
        self.assertNotIn("https://cdn.example.com/media/", csp)

    def test_relative_media_url_not_added_to_frame_src(self):
        csp = self._make_response()["Content-Security-Policy"]
        self.assertNotIn("/media/", csp)

    def test_existing_policy_not_overridden(self):
        existing = "frame-src 'none'"

        def get_response(request):
            resp = HttpResponse("ok")
            resp["Content-Security-Policy"] = existing
            return resp

        middleware = ContentSecurityPolicyMiddleware(get_response)
        self.assertEqual(middleware(None)["Content-Security-Policy"], existing)
