from django.contrib.auth.models import User
from django.test import TestCase

from judge.authentication import CustomModelBackend


class CustomModelBackendTest(TestCase):
    def setUp(self):
        self.backend = CustomModelBackend()

    def test_authenticates_by_email_when_username_is_missing(self):
        user = User.objects.create_user(
            username="email_login_user",
            email="email-login@example.com",
            password="password123",
        )

        authenticated = self.backend.authenticate(
            None, username="email-login@example.com", password="password123"
        )

        self.assertEqual(authenticated, user)

    def test_username_match_takes_precedence_over_email_match(self):
        username_user = User.objects.create_user(
            username="shared@example.com",
            email="username-owner@example.com",
            password="username-password",
        )
        User.objects.create_user(
            username="email_owner",
            email="shared@example.com",
            password="email-password",
        )

        authenticated = self.backend.authenticate(
            None, username="shared@example.com", password="username-password"
        )
        wrong_password_for_username = self.backend.authenticate(
            None, username="shared@example.com", password="email-password"
        )

        self.assertEqual(authenticated, username_user)
        self.assertIsNone(wrong_password_for_username)

    def test_inactive_user_is_not_authenticated(self):
        User.objects.create_user(
            username="inactive_login_user",
            password="password123",
            is_active=False,
        )

        authenticated = self.backend.authenticate(
            None, username="inactive_login_user", password="password123"
        )

        self.assertIsNone(authenticated)
