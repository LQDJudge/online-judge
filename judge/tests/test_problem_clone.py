from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.test import RequestFactory, TestCase

from judge.forms import ProblemCloneForm
from judge.models import Language, Organization, Problem, ProblemGroup, Profile
from judge.views.problem import ProblemClone


class ProblemCloneOrganizationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        language, _ = Language.objects.get_or_create(
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
        group, _ = ProblemGroup.objects.get_or_create(
            name="clone-test", defaults={"full_name": "Clone Test"}
        )
        cls.user = User.objects.create_user(username="problem_cloner")
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": language}
        )
        cls.source = Problem.objects.create(
            code="clone-source",
            name="Clone source",
            group=group,
            time_limit=1.0,
            memory_limit=262144,
            points=1.0,
            is_public=True,
        )

    def clone(self, code):
        request = RequestFactory().post("/problem/clone-source/clone", {"code": code})
        request.user = self.user
        request.profile = self.profile
        form = ProblemCloneForm(request.POST)
        self.assertTrue(form.is_valid())
        view = ProblemClone()
        view.request = request
        view.object = self.source
        view.form_valid(form)
        return Problem.objects.get(code=code)

    def test_clone_copies_organizations_and_keeps_restriction(self):
        organizations = [
            Organization.objects.create(
                name=f"Clone Org {number}",
                slug=f"clone-org-{number}",
                short_name=f"CO{number}",
                about="Test organization",
                registrant=self.profile,
                is_open=True,
            )
            for number in (1, 2)
        ]
        self.source.organizations.set(organizations)

        clone = self.clone("cloneorg")

        self.assertFalse(clone.is_public)
        self.assertTrue(clone.is_organization_private)
        self.assertCountEqual(
            clone.organizations.values_list("id", flat=True),
            [organization.id for organization in organizations],
        )
        self.assertCountEqual(
            self.source.organizations.values_list("id", flat=True),
            [organization.id for organization in organizations],
        )

    def test_clone_clears_stale_restriction_without_organizations(self):
        self.source.is_organization_private = True
        self.source.save(update_fields=["is_organization_private"])

        clone = self.clone("clonenorg")

        self.assertFalse(clone.is_organization_private)
        self.assertFalse(clone.organizations.exists())
        clone.is_public = True
        clone.save(update_fields=["is_public"])
        self.assertTrue(clone.is_accessible_by(AnonymousUser()))

    def test_failed_organization_copy_rolls_back_clone(self):
        organization = Organization.objects.create(
            name="Clone Rollback Org",
            slug="clone-rollback-org",
            short_name="CRO",
            about="Test organization",
            registrant=self.profile,
            is_open=True,
        )
        self.source.organizations.add(organization)

        with patch.object(
            type(self.source.organizations),
            "set",
            side_effect=RuntimeError("copy failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                self.clone("clonefail")

        self.assertFalse(Problem.objects.filter(code="clonefail").exists())
