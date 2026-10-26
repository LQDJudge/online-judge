import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Judge,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
    SubmissionSource,
)
from judge.models.problem_data import ProblemData, ProblemSolutionCode
from judge.review.snapshots import _capture_output_examples
from judge.forms import ProblemSubmitForm


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
        self.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
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

    def test_output_portfolio_shows_verdict_and_score_without_source_editor(self):
        output = Language.objects.create(
            key="OUTPUT",
            name="Output only",
            short_name="OUT",
            ace="text",
            pygments="text",
        )
        ProblemData.objects.create(problem=self.problem, output_only=True)
        Submission.objects.create(
            problem=self.problem,
            user=self.profile,
            language=output,
            status="D",
            result="WA",
            case_points=1,
            case_total=2,
            review_evidence={"output_portfolio": True},
        )
        response = self.client.get(
            reverse("problem_solution_codes", args=[self.problem.code])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<span class="orange">WA</span>', html=True)
        self.assertContains(response, "1 / 2")
        self.assertNotContains(response, 'id="add-code-btn"')

    def test_failed_output_download_does_not_enter_the_review_portfolio(self):
        output = Language.objects.create(
            key="OUTPUT",
            name="Output only",
            short_name="OUT",
            ace="text",
            pygments="text",
        )
        failed = Submission.objects.create(
            problem=self.problem,
            user=self.profile,
            language=output,
            status="CE",
            result="CE",
            review_evidence={"output_portfolio": True},
        )
        SubmissionSource.objects.create(
            submission=failed,
            source="http://unavailable.example/submission_source_file/example.zip",
        )
        with patch(
            "judge.review.snapshots.open_output_source",
            side_effect=AssertionError("Failed download entered review evidence"),
        ):
            self.assertEqual(_capture_output_examples(self.problem, False, True), [])

    def test_invalid_submission_fields_do_not_crash_or_write_uploads(self):
        form = ProblemSubmitForm(
            data={"language": "not-a-language", "source": "upload"},
            files={"source_file": SimpleUploadedFile("outputs.zip", b"invalid")},
            problem=self.problem,
            request=RequestFactory().post("/"),
        )
        with patch("judge.forms.default_storage.save") as save:
            self.assertFalse(form.is_valid())
        self.assertIn("language", form.errors)
        save.assert_not_called()

    def test_file_runtimes_only_fetch_registered_site_uploads(self):
        for key in ("OUTPUT", "SCAT"):
            language = Language.objects.create(
                key=key, name=key, short_name=key, ace="text", pygments="text"
            )
            self.judge.runtimes.add(language)
            for source, valid in (
                ("http://169.254.169.254/latest/meta-data/", False),
                ("https://untrusted.invalid/submission_source_file/example.zip", True),
            ):
                with self.subTest(key=key, source=source):
                    form = ProblemSubmitForm(
                        data={"language": language.pk, "source": source},
                        problem=self.problem,
                        request=RequestFactory().post("/"),
                    )
                    self.assertEqual(form.is_valid(), valid)
                    if valid:
                        self.assertEqual(
                            form.cleaned_data["source"],
                            "http://testserver/submission_source_file/example.zip",
                        )
            malformed = ProblemSubmitForm(
                data={
                    "language": language.pk,
                    "source": "upload",
                    "uploaded_file_key": "submissions/../private",
                },
                problem=self.problem,
                request=RequestFactory().post("/"),
            )
            self.assertFalse(malformed.is_valid())

    def test_solution_code_endpoints_cannot_run_file_download_urls(self):
        for key in ("OUTPUT", "SCAT"):
            language = Language.objects.create(
                key=key, name=key, short_name=key, ace="text", pygments="text"
            )
            self.judge.runtimes.add(language)
            self.problem.allowed_languages.add(language)
            with self.subTest(key=key), patch.object(Submission, "judge") as judge:
                saved = self.client.post(
                    reverse("problem_solution_codes_save", args=[self.problem.code]),
                    json.dumps(
                        [
                            {
                                "name": "download",
                                "source_code": "http://169.254.169.254/",
                                "language_id": language.pk,
                                "expected_result": "AC",
                            }
                        ]
                    ),
                    content_type="application/json",
                )
                self.assertEqual(saved.status_code, 400)
                self.solution_code.language = language
                self.solution_code.save(update_fields=["language"])
                for route, body in (
                    ("problem_solution_codes_run", {}),
                    ("problem_solution_codes_run_one", {"order": 0}),
                ):
                    response = self.client.post(
                        reverse(route, args=[self.problem.code]),
                        json.dumps(body),
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 400)
                judge.assert_not_called()
        self.assertFalse(Submission.objects.exists())

    def test_malformed_solution_code_entries_return_validation_errors(self):
        for entry in ("not-an-object", {"name": ["bad"]}, {"source_code": 1}):
            response = self.client.post(
                reverse("problem_solution_codes_save", args=[self.problem.code]),
                json.dumps([entry]),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)

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

    def test_dispatch_failure_does_not_leave_solution_code_queued(self):
        with patch(
            "judge.judgeapi.delete_submission_result_async.delay",
            side_effect=ConnectionError("local broker unavailable"),
        ), patch("judge.judgeapi.judge_request") as request:
            response = self.client.post(
                reverse("problem_solution_codes_run", args=[self.problem.code])
            )
        self.assertEqual(response.status_code, 200)
        self.solution_code.refresh_from_db()
        submission = self.solution_code.last_submission
        self.assertEqual(submission.status, "IE")
        self.assertEqual(submission.result, "IE")
        request.assert_not_called()
        result = self.client.get(
            reverse("problem_solution_codes_status", args=[self.problem.code])
        ).json()
        self.assertTrue(result["all_graded"])
        self.assertTrue(result["results"][0]["is_graded"])
        self.assertFalse(result["results"][0]["match"])

    def test_terminal_compile_error_and_abort_stop_polling(self):
        submission = Submission.objects.create(
            problem=self.problem,
            user=self.profile,
            language=self.language,
            status="CE",
            result="CE",
        )
        self.solution_code.last_submission = submission
        self.solution_code.save(update_fields=["last_submission"])
        for status in ("CE", "AB"):
            with self.subTest(status=status):
                submission.status = status
                submission.result = status
                submission.save(update_fields=["status", "result"])
                result = self.client.get(
                    reverse("problem_solution_codes_status", args=[self.problem.code])
                ).json()
                self.assertTrue(result["all_graded"])
                self.assertTrue(result["results"][0]["is_graded"])
                self.assertFalse(result["results"][0]["match"])

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

    def test_solution_code_page_warns_before_judge_has_problem(self):
        self.judge.problems.remove(self.problem)
        self.assert_problem_not_ready()

    def test_solution_code_page_warns_when_judge_is_offline(self):
        self.judge.online = False
        self.judge.save(update_fields=["online"])
        self.assert_problem_not_ready()

    def assert_problem_not_ready(self):
        response = self.client.get(
            reverse("problem_solution_codes", args=[self.problem.code]),
            HTTP_ACCEPT_LANGUAGE="en",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["languages"], [])
        self.assertContains(response, 'role="alert"')
        self.assertContains(response, "This problem is not ready for submissions yet.")

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
