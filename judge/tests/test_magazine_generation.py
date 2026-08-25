from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge.management.commands.generate_magazine_posts import Command
from judge.models import (
    Contest,
    ContestProblem,
    Language,
    Organization,
    Problem,
    ProblemGroup,
    Profile,
)


class MagazineGenerationPublicContentTests(TestCase):
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
        cls.problem_group, _ = ProblemGroup.objects.get_or_create(
            name="magazine",
            defaults={"full_name": "Magazine"},
        )
        cls.user = User.objects.create_user("magazine_user")
        cls.profile, _ = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": cls.language}
        )

    def _problem(self, code, *, is_public=True, is_organization_private=False):
        return Problem.objects.create(
            code=code,
            name="Problem %s" % code,
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=262144,
            points=10.0,
            is_public=is_public,
            is_organization_private=is_organization_private,
            description="Problem statement for %s" % code,
            user_count=20,
        )

    def _contest(self, key, **kwargs):
        now = timezone.now()
        defaults = {
            "name": "Contest %s" % key,
            "start_time": now - timedelta(hours=3),
            "end_time": now - timedelta(hours=1),
            "is_visible": True,
            "is_private": False,
            "is_organization_private": False,
            "is_in_course": False,
        }
        defaults.update(kwargs)
        return Contest.objects.create(key=key, **defaults)

    def test_public_contest_queryset_excludes_scoped_contests(self):
        public_contest = self._contest("public")
        self._contest("hidden", is_visible=False)
        self._contest("private-users", is_private=True)
        self._contest("private-org", is_organization_private=True)
        self._contest("course-contest", is_in_course=True)

        contests = list(Command()._base_public_contest_queryset())

        self.assertEqual(contests, [public_contest])

    def test_contest_discussion_org_mixed_plan_is_contest_only(self):
        org = Organization.objects.create(
            name="Thảo luận kỳ thi",
            slug="thao-luan-ky-thi",
            short_name="TLKT",
            about="Nơi thảo luận các kỳ thi.",
            registrant=self.profile,
            is_community=True,
        )

        plan = Command()._mixed_post_plan(3, None, org)

        self.assertEqual(plan, ["contest", "contest", "contest"])

    def test_contest_prompt_uses_only_public_problems(self):
        contest = self._contest("contest")
        public_problem = self._problem("public-problem")
        private_problem = self._problem("private-problem", is_public=False)
        org_private_problem = self._problem(
            "org-private-problem", is_organization_private=True
        )
        ContestProblem.objects.create(
            contest=contest, problem=public_problem, points=100, order=1
        )
        ContestProblem.objects.create(
            contest=contest, problem=private_problem, points=100, order=2
        )
        ContestProblem.objects.create(
            contest=contest, problem=org_private_problem, points=100, order=3
        )
        command = Command()

        with patch.object(
            command, "_call_with_validation", return_value="generated content"
        ) as call_with_validation:
            command._generate_contest_post(None, contest)

        prompt = call_with_validation.call_args.args[1]
        self.assertIn("public-problem", prompt)
        self.assertNotIn("private-problem", prompt)
        self.assertNotIn("org-private-problem", prompt)
