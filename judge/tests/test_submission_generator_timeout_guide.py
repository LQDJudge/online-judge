from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import (
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
    SubmissionSource,
)
from judge.views.submission import (
    is_checker_timeout_error,
    is_generator_timeout_error,
)


@override_settings(LANGUAGE_CODE="en")
class SubmissionGeneratorTimeoutGuideTests(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.language = Language.objects.first()
        self.group = ProblemGroup.objects.create(name="gt", full_name="Generator Tests")
        self.author = self._profile("generator_author")
        self.submitter = self._profile("generator_submitter")
        self.problem = Problem.objects.create(
            code="gentle",
            name="Generator Timeout",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100,
        )
        self.problem.authors.add(self.author)

    def _profile(self, username):
        user = User.objects.create_user(username=username, password="password")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        return profile

    def _submission(self, error="", status="IE", result="IE"):
        submission = Submission.objects.create(
            user=self.submitter,
            problem=self.problem,
            language=self.language,
            status=status,
            result=result,
            error=error,
            points=100 if status == "D" else None,
            case_points=100 if status == "D" else 0,
            case_total=100 if status == "D" else 0,
            time=0.1 if status == "D" else None,
            memory=1024 if status == "D" else None,
        )
        SubmissionSource.objects.create(submission=submission, source="print('hi')")
        return submission

    def test_generator_timeout_error_detection(self):
        self.assertTrue(
            is_generator_timeout_error(
                "dmoj.error.InternalError: generator timed out (> 20 seconds)"
            )
        )
        self.assertFalse(is_generator_timeout_error("Judge worker timeout"))

    def test_checker_timeout_error_detection(self):
        self.assertTrue(
            is_checker_timeout_error(
                "dmoj.error.InternalError: checker timed out (> 20 seconds)"
            )
        )
        self.assertFalse(is_checker_timeout_error("Judge worker timeout"))

    def test_problem_author_sees_generator_timeout_guide(self):
        submission = self._submission(
            "Traceback\n" "dmoj.error.InternalError: generator timed out (> 20 seconds)"
        )
        self.client.login(username=self.author.user.username, password="password")

        response = self.client.get(reverse("submission_status", args=[submission.id]))

        self.assertContains(response, "Generator timed out")
        self.assertContains(response, "Tips")
        self.assertContains(response, "Test data instructions")
        self.assertContains(
            response, reverse("test_data_instructions") + "#test-generator"
        )
        self.assertContains(response, "ios::sync_with_stdio(false)")
        self.assertContains(response, "Use the fastest accepted solution")
        self.assertContains(response, "Open test data")
        self.assertContains(response, reverse("problem_data", args=[self.problem.code]))

    def test_submitter_does_not_see_generator_timeout_guide(self):
        submission = self._submission(
            "dmoj.error.InternalError: generator timed out (> 20 seconds)"
        )
        self.client.login(username=self.submitter.user.username, password="password")

        response = self.client.get(reverse("submission_status", args=[submission.id]))

        self.assertNotContains(response, "Test data instructions")
        self.assertNotContains(response, "ios::sync_with_stdio(false)")
        self.assertNotContains(response, "Open test data")

    def test_problem_author_does_not_see_guide_for_other_internal_error(self):
        submission = self._submission("Judge worker timeout")
        self.client.login(username=self.author.user.username, password="password")

        response = self.client.get(reverse("submission_status", args=[submission.id]))

        self.assertNotContains(response, "Generator timed out")

    def test_problem_author_sees_checker_timeout_guide(self):
        submission = self._submission(
            "Traceback\n" "dmoj.error.InternalError: checker timed out (> 20 seconds)"
        )
        self.client.login(username=self.author.user.username, password="password")

        response = self.client.get(reverse("submission_status", args=[submission.id]))

        self.assertContains(response, "Checker timed out")
        self.assertContains(response, "Tips")
        self.assertContains(response, "Test data instructions")
        self.assertContains(response, reverse("test_data_instructions") + "#checker")
        self.assertContains(response, "Avoid reading or parsing")
        self.assertContains(response, "split rows")
        self.assertContains(response, "Open test data")

    def test_submitter_does_not_see_checker_timeout_guide(self):
        submission = self._submission(
            "dmoj.error.InternalError: checker timed out (> 20 seconds)"
        )
        self.client.login(username=self.submitter.user.username, password="password")

        response = self.client.get(reverse("submission_status", args=[submission.id]))

        self.assertNotContains(response, "Checker timed out")
        self.assertNotContains(response, "Avoid reading or parsing")
