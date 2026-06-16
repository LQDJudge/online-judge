from datetime import timedelta

from django.contrib.auth.models import AnonymousUser, Permission, User
from django.test import TestCase, override_settings
from django.utils import timezone

from judge.models import Contest, Language, Profile


def _make_language():
    lang, _ = Language.objects.get_or_create(
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
    return lang


def _profile(username, language, is_superuser=False, add_problem=False):
    if is_superuser:
        user = User.objects.create_superuser(username, f"{username}@x.com", "pw")
    else:
        user = User.objects.create_user(username, f"{username}@x.com", "pw")
    if add_problem:
        user.user_permissions.add(Permission.objects.get(codename="add_problem"))
    profile, _ = Profile.objects.get_or_create(
        user=user, defaults={"language": language}
    )
    # Re-fetch so the permission cache reflects the granted perm.
    return Profile.objects.get(pk=profile.pk)


@override_settings(LANGUAGE_CODE="en")
class ContestCanRequestPublicTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        cls.superuser = _profile("crp_admin", cls.language, is_superuser=True)
        cls.qualified_author = _profile("crp_qauth", cls.language, add_problem=True)
        cls.unqualified_author = _profile("crp_uauth", cls.language, add_problem=False)
        cls.qualified_outsider = _profile("crp_qout", cls.language, add_problem=True)

        now = timezone.now()
        cls.contest = Contest.objects.create(
            key="crpc",
            name="CRP Contest",
            description="x",
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_visible=False,
            is_rated=False,
            format_name="default",
        )
        cls.contest.authors.add(cls.qualified_author)
        cls.contest.authors.add(cls.unqualified_author)

    def _can(self, profile):
        # Pass a fresh User so has_perm uses an up-to-date permission cache.
        user = User.objects.get(pk=profile.user_id)
        return self.contest.can_request_public_by(user)

    def test_superuser_can(self):
        self.assertTrue(self._can(self.superuser))

    def test_editor_with_add_problem_can(self):
        self.assertTrue(self._can(self.qualified_author))

    def test_editor_without_add_problem_cannot(self):
        # Author of the contest but lacks the setter qualification.
        self.assertFalse(self._can(self.unqualified_author))

    def test_qualified_non_editor_cannot(self):
        # Has the qualification but isn't an editor of this contest.
        self.assertFalse(self._can(self.qualified_outsider))

    def test_anonymous_cannot(self):
        self.assertFalse(self.contest.can_request_public_by(AnonymousUser()))


@override_settings(LANGUAGE_CODE="en", AUTO_REVIEW_CONTEST_ENABLED=True)
class ContestRequestPublicEndpointPermissionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language = _make_language()
        cls.unqualified_author = _profile("ep_uauth", cls.language, add_problem=False)
        now = timezone.now()
        cls.contest = Contest.objects.create(
            key="epc",
            name="EP Contest",
            description="x",
            start_time=now,
            end_time=now + timedelta(hours=3),
            is_visible=False,
            is_rated=False,
            format_name="default",
        )
        cls.contest.authors.add(cls.unqualified_author)

    def test_editor_without_qualification_is_denied(self):
        self.client.force_login(self.unqualified_author.user)
        resp = self.client.post("/contest/epc/review/request_public", {})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["success"])
        self.assertIn("permission", data["error"].lower())
