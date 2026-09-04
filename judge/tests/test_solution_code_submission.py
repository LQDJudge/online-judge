import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import Judge, Language, Problem, ProblemGroup, Profile, Submission
from judge.models.problem_data import ProblemSolutionCode


class SolutionCodeSubmissionTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.language = Language.objects.get(key="PY3")
        self.group = ProblemGroup.objects.create(
            name="solution-code-submit", full_name="Solution code submission"
        )
        self.problem = Problem.objects.create(
            code="solutioncodesubmit",
            name="Solution code submission",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100,
        )
        self.problem.allowed_languages.add(self.language)

        user = User.objects.create_user("solution-code-author", password="pw")
        self.profile, _ = Profile.objects.get_or_create(user=user)
        self.profile.language = self.language
        self.profile.save(update_fields=["language"])
        self.problem.authors.add(self.profile)
        self.client.force_login(user)

        self.judge = Judge.objects.create(
            name="solution-code-judge",
            auth_key="key",
            online=True,
            start_time=timezone.now(),
            ping=0,
            load=0,
        )
        self.judge.runtimes.add(self.language)
        self.judge.problems.add(self.problem)
        self.solution_code = ProblemSolutionCode.objects.create(
            problem=self.problem,
            order=0,
            source_code="print(1)",
            language=self.language,
            expected_result="AC",
        )

    def test_run_all_rejects_language_disabled_for_problem(self):
        self.problem.allowed_languages.remove(self.language)

        with patch.object(Submission, "judge") as judge:
            response = self.client.post(
                reverse("problem_solution_codes_run", args=[self.problem.code])
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")
        self.assertFalse(Submission.objects.exists())
        judge.assert_not_called()

    def test_solution_code_page_only_lists_usable_languages(self):
        other_language = Language.objects.get(key="CPP17")
        self.judge.runtimes.add(other_language)

        response = self.client.get(
            reverse("problem_solution_codes", args=[self.problem.code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {language["id"] for language in response.context["languages"]},
            {self.language.id},
        )

    def test_run_one_rejects_language_when_judge_is_offline(self):
        self.judge.online = False
        self.judge.save(update_fields=["online"])

        with patch.object(Submission, "judge") as judge:
            response = self.client.post(
                reverse("problem_solution_codes_run_one", args=[self.problem.code]),
                data=json.dumps({"order": self.solution_code.order}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["status"], "error")
        self.assertFalse(Submission.objects.exists())
        judge.assert_not_called()

    def test_run_one_submits_when_language_is_usable(self):
        with patch.object(Submission, "judge") as judge:
            response = self.client.post(
                reverse("problem_solution_codes_run_one", args=[self.problem.code]),
                data=json.dumps({"order": self.solution_code.order}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Submission.objects.count(), 1)
        judge.assert_called_once_with(rejudge=False, batch_rejudge=True)
