from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase

from judge.jinja2.gravatar import default_gravatar, gravatar
from judge.models import Language, Profile


class GravatarTest(TestCase):
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

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def create_profile(self, username="muted_avatar_user", **profile_kwargs):
        user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password="password",
        )
        return Profile.objects.create(
            user=user,
            language=self.language,
            **profile_kwargs,
        )

    def test_muted_profile_keeps_uploaded_avatar(self):
        profile = self.create_profile(
            mute=True,
            profile_image="profile_images/avatar.png",
        )
        expected_url = profile.profile_image.url

        self.assertEqual(gravatar(profile.id), expected_url)

    def test_gravatar_accepts_profile_instance(self):
        profile = self.create_profile(username="profile_instance_avatar")

        self.assertEqual(gravatar(profile), gravatar(profile.id))

    def test_muted_profile_uses_normal_gravatar_fallback(self):
        profile = self.create_profile(username="muted_without_avatar", mute=True)
        url = gravatar(profile.id, size=120)

        self.assertNotEqual(url, default_gravatar(120))
        self.assertIn("d=identicon", url)
        self.assertIn("s=120", url)
        self.assertNotIn("f=y", url)
