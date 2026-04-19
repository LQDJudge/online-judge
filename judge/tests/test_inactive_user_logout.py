from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from judge.middleware import InactiveUserLogoutMiddleware


class InactiveUserLogoutMiddlewareTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="alice", email="alice@example.com", password="pw"
        )

    def _make_request(self, user):
        request = self.factory.get("/")
        request.session = self.client.session
        request.user = user
        return request

    def test_active_user_passes_through(self):
        request = self._make_request(self.user)
        middleware = InactiveUserLogoutMiddleware(lambda req: HttpResponse("ok"))

        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(request.user, self.user)

    def test_anonymous_user_passes_through(self):
        request = self._make_request(AnonymousUser())
        middleware = InactiveUserLogoutMiddleware(lambda req: HttpResponse("ok"))

        response = middleware(request)

        self.assertEqual(response.status_code, 200)

    def test_inactive_user_is_logged_out(self):
        self.user.is_active = False
        self.user.save()

        request = self._make_request(self.user)
        middleware = InactiveUserLogoutMiddleware(lambda req: HttpResponse("ok"))

        response = middleware(request)

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(request.user, AnonymousUser)


class InactiveUserLogoutIntegrationTest(TestCase):
    """Verify the middleware is wired into the real middleware stack."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="bob", email="bob@example.com", password="pw"
        )

    def test_session_flushed_when_is_active_flips_false(self):
        self.client.force_login(self.user)
        self.assertIn(SESSION_KEY, self.client.session)

        self.user.is_active = False
        self.user.save()

        # Any request triggers the middleware; we only care that the session
        # was flushed, not what the view returned.
        self.client.get("/")

        self.assertNotIn(SESSION_KEY, self.client.session)
