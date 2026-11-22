"""Contest hash batching preserves per-problem identity and bounds DB queries."""

from datetime import timedelta
import io
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from judge.models import (
    Contest,
    ContestProblem,
    Language,
    Problem,
    ProblemData,
    ProblemGroup,
    ProblemSolutionCode,
    ProblemTestCase,
    ProblemValidation,
    Profile,
    Submission,
    SubmissionSource,
    SubmissionTestCase,
    ProblemReviewRun,
    PublicRequest,
    ContestReviewRun,
    ContestReviewDependency,
    ContestPublicRequest,
)
from judge.models.problem_data import ProblemSignatureGrader, ProblemValidationResult
from judge.models.problem import LanguageLimit, LanguageTemplate
from judge.auto_review.problem_context import (
    _batch_problem_identities,
    _capture_problem,
    hash_current_problem,
)
from judge.auto_review.contest_context import build_contest_context
from judge.auto_review.review_context import current_content_hash
from judge.auto_review.signature import relevant_signature_config
from judge.auto_review.execution_freshness import (
    solution_execution_hash,
    solution_result_current,
)
from judge.management.commands import transition_review_hashes as hash_transition


@override_settings(USE_ML=False)
class ContestHashBatchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
            },
        )
        user = User.objects.create_user("batch_identity", password="test")
        cls.author, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.group = ProblemGroup.objects.create(
            name="hashbatch", full_name="Hash batch"
        )
        cls.contest = Contest.objects.create(
            key="hashbatch",
            name="Hash batch",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=2),
        )

    def make_problem(self, code):
        problem = Problem.objects.create(
            code=code,
            name=code,
            description="Statement",
            group=self.group,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        problem.allowed_languages.add(self.language)
        LanguageLimit.objects.create(
            problem=problem, language=self.language, time_limit=2, memory_limit=131072
        )
        LanguageTemplate.objects.create(
            problem=problem, language=self.language, source="template"
        )
        ProblemData.objects.create(problem=problem, checker="standard")
        ProblemTestCase.objects.create(
            dataset=problem,
            order=0,
            type="C",
            input_file="a.in",
            output_file="a.out",
            is_pretest=False,
        )
        submission = Submission.objects.create(
            problem=problem,
            user=self.author,
            language=self.language,
            status="D",
            result="AC",
            execution_hash="stored",
        )
        SubmissionSource.objects.create(submission=submission, source="print(1)")
        SubmissionTestCase.objects.create(
            submission=submission, case=1, status="AC", points=1, total=1
        )
        ProblemSolutionCode.objects.create(
            problem=problem,
            source_code="print(1)",
            language=self.language,
            expected_result="AC",
            last_submission=submission,
        )
        validation = ProblemValidation.objects.create(
            problem=problem,
            user=self.author,
            validate_id=code,
            status="D",
            passed=True,
            total_cases=1,
            execution_hash="stored",
        )
        ProblemValidationResult.objects.create(
            validation=validation, case=1, status="AC"
        )
        return problem

    def test_hash_transition_preserves_only_provably_current_evidence(self):
        problem = self.make_problem("hashtransition")
        problem.refresh_from_db()
        code = problem.solution_codes.select_related("last_submission").get()
        submission = code.last_submission
        submission.execution_hash = hash_transition._old_solution_execution_hash(
            problem, code.source_code, code.language_id
        )
        submission.save(update_fields=["execution_hash"])
        context = _capture_problem(problem, extract_pdf=False, identity_only=True)
        old_hash = hash_transition._old_problem_review_input_hash(context)
        current = ProblemReviewRun.objects.create(
            problem=problem, status="D", input_hash=old_hash
        )
        stale = ProblemReviewRun.objects.create(
            problem=problem, status="D", input_hash="stale"
        )
        request = PublicRequest.objects.create(
            problem=problem,
            requested_by=self.author,
            review_run=current,
            candidate_hash=old_hash,
        )
        call_command("transition_review_hashes", stdout=io.StringIO())
        current.refresh_from_db()
        self.assertEqual(current.input_hash, old_hash)
        output = io.StringIO()
        call_command("transition_review_hashes", apply=True, stdout=output)
        current.refresh_from_db()
        stale.refresh_from_db()
        submission.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(current.input_hash, hash_current_problem(problem))
        self.assertEqual(request.candidate_hash, current.input_hash)
        self.assertEqual(stale.input_hash, "stale")
        self.assertTrue(
            solution_result_current(problem, code, submission), output.getvalue()
        )

    def test_transition_updates_current_contest_dependency_and_pending_request(self):
        problem = self.make_problem("contesttransition")
        problem.refresh_from_db()
        ContestProblem.objects.create(
            contest=self.contest, problem=problem, order=1, points=100
        )
        context = _capture_problem(problem, extract_pdf=False, identity_only=True)
        old_problem = hash_transition._old_problem_review_input_hash(context)
        child = ProblemReviewRun.objects.create(
            problem=problem, status="D", input_hash=old_problem
        )
        contest_context = build_contest_context(
            self.contest,
            [{"problem_id": problem.pk, "content_hash": old_problem}],
            identity_only=True,
        )
        old_contest = hash_transition.digest_json(
            hash_transition._old_contest_review_content(contest_context)
        )
        run = ContestReviewRun.objects.create(
            contest=self.contest, status="D", input_hash=old_contest
        )
        dependency = ContestReviewDependency.objects.create(
            contest_run=run,
            problem=problem,
            problem_run=child,
            content_hash=old_problem,
        )
        request = ContestPublicRequest.objects.create(
            contest=self.contest,
            requested_by=self.author,
            review_run=run,
            candidate_hash=old_contest,
        )
        call_command("transition_review_hashes", apply=True, stdout=io.StringIO())
        run.refresh_from_db()
        dependency.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(run.input_hash, current_content_hash(self.contest))
        self.assertEqual(dependency.content_hash, hash_current_problem(problem))
        self.assertEqual(request.candidate_hash, run.input_hash)

    def test_batched_hash_matches_individual_for_execution_and_modes(self):
        problems = [
            self.make_problem("batchnormal"),
            self.make_problem("batchsignature"),
            self.make_problem("batchoutput"),
        ]
        signature = ProblemData.objects.get(problem=problems[1])
        signature.use_ioi_signature = True
        signature.save()
        ProblemSignatureGrader.objects.create(
            problem=problems[1], language="python", handler="", header=None
        )
        output = ProblemData.objects.get(problem=problems[2])
        output.output_only = True
        output.save()
        batch = _batch_problem_identities([p.pk for p in problems])
        for problem in problems:
            with self.subTest(problem=problem.code):
                self.assertEqual(
                    hash_current_problem(problem, batch=batch),
                    hash_current_problem(problem),
                )
                batched = _capture_problem(
                    problem, extract_pdf=False, identity_only=True, batch=batch
                )
                individual = _capture_problem(
                    problem, extract_pdf=False, identity_only=True
                )
                self.assertEqual(batched["solutions"], individual["solutions"])
                self.assertEqual(batched["validation"], individual["validation"])

    def test_difficulty_and_pretest_selection_do_not_invalidate_full_execution(self):
        problem = self.make_problem("irrelevantchanges")
        problem.refresh_from_db()
        code = problem.solution_codes.get()
        before_review = hash_current_problem(problem)
        before_execution = solution_execution_hash(
            problem, code.source_code, code.language_id
        )
        Problem.objects.filter(pk=problem.pk).update(points=1500)
        ProblemTestCase.objects.filter(dataset=problem).update(is_pretest=True)
        problem.refresh_from_db()
        self.assertEqual(before_review, hash_current_problem(problem))
        self.assertEqual(
            before_execution,
            solution_execution_hash(problem, code.source_code, code.language_id),
        )

    def test_contest_hash_query_count_does_not_grow_per_problem(self):
        first = self.make_problem("batchfirst")
        ContestProblem.objects.create(
            contest=self.contest, problem=first, order=1, points=10
        )
        with CaptureQueriesContext(connection) as single:
            current_content_hash(self.contest)
        for number in range(2, 6):
            problem = self.make_problem("batch%d" % number)
            ContestProblem.objects.create(
                contest=self.contest, problem=problem, order=number, points=10
            )
        with CaptureQueriesContext(connection) as multiple:
            current_content_hash(self.contest)
        self.assertLessEqual(
            len(multiple), len(single) + 1, (len(single), len(multiple))
        )

    def test_contest_hash_matches_individual_hashes_and_changes_with_evidence(self):
        problems = [
            self.make_problem("batchevidence1"),
            self.make_problem("batchevidence2"),
        ]
        for order, problem in enumerate(problems):
            ContestProblem.objects.create(
                contest=self.contest, problem=problem, order=order, points=10
            )
        children = [
            {"problem_id": p.pk, "content_hash": hash_current_problem(p)}
            for p in problems
        ]
        original = build_contest_context(self.contest, children, identity_only=True)[
            "input_hash"
        ]
        self.assertEqual(current_content_hash(self.contest), original)
        submission = problems[0].solution_codes.first().last_submission
        submission.test_cases.filter(case=1).update(status="WA", points=0)
        self.assertNotEqual(current_content_hash(self.contest), original)

    def test_output_portfolio_matches_and_keeps_only_latest_twelve(self):
        problem = self.make_problem("batchportfolio")
        data = ProblemData.objects.get(problem=problem)
        data.output_only = True
        data.save()
        language, _ = Language.objects.get_or_create(
            key="OUTPUT",
            defaults={
                "name": "Output",
                "short_name": "OUT",
                "ace": "text",
                "pygments": "text",
            },
        )
        with tempfile.TemporaryDirectory() as root:
            storage = FileSystemStorage(location=root)
            storage.save("submissions/example.zip", ContentFile(b"example"))
            for number in range(13):
                submission = Submission.objects.create(
                    problem=problem,
                    user=self.author,
                    language=language,
                    status="D",
                    result="WA",
                    review_evidence={"output_portfolio": True},
                    case_points=number,
                    case_total=13,
                )
                SubmissionSource.objects.create(
                    submission=submission, source="/submission_source_file/example.zip"
                )
                SubmissionTestCase.objects.create(
                    submission=submission, case=1, status="WA", points=number, total=13
                )
            with patch(
                "judge.auto_review.problem_context.default_storage", storage
            ), patch("judge.auto_review.artifact_readers.default_storage", storage):
                batch = _batch_problem_identities([problem.pk])
                batched = _capture_problem(
                    problem, extract_pdf=False, identity_only=True, batch=batch
                )
                individual = _capture_problem(
                    problem, extract_pdf=False, identity_only=True
                )
        self.assertEqual(len(batched["output_examples"]), 12)
        self.assertEqual(batched["output_examples"], individual["output_examples"])
        self.assertEqual(batched["input_hash"], individual["input_hash"])

    def test_transition_converts_only_current_output_portfolio_results(self):
        problem = self.make_problem("outputtransition")
        problem.refresh_from_db()
        language, _ = Language.objects.get_or_create(
            key="OUTPUT",
            defaults={
                "name": "Output",
                "short_name": "OUT",
                "ace": "text",
                "pygments": "text",
            },
        )
        submissions = []
        for execution_hash in ("current", "stale"):
            submission = Submission.objects.create(
                problem=problem,
                user=self.author,
                language=language,
                status="D",
                result="AC",
                review_evidence={"output_portfolio": True},
            )
            source = "/submission_source_file/example.zip"
            SubmissionSource.objects.create(submission=submission, source=source)
            submission.execution_hash = (
                hash_transition._old_solution_execution_hash(
                    problem, source, language.pk
                )
                if execution_hash == "current"
                else "stale"
            )
            submission.save(update_fields=["execution_hash"])
            submissions.append(submission)
        call_command("transition_review_hashes", apply=True, stdout=io.StringIO())
        for submission in submissions:
            submission.refresh_from_db()
        self.assertEqual(
            submissions[0].execution_hash,
            solution_execution_hash(problem, source, language.pk),
        )
        self.assertEqual(submissions[1].execution_hash, "stale")

    def test_signature_pretest_configuration_does_not_change_review_hash(self):
        problem = self.make_problem("signaturepretest")
        data = ProblemData.objects.get(problem=problem)
        data.use_ioi_signature = True
        data.save()
        with tempfile.TemporaryDirectory() as root:
            storage = FileSystemStorage(location=root)
            name = "signaturepretest/init.yml"
            storage.save(name, ContentFile(b"signature_grader: {}\ntest_cases: []\n"))
            with patch(
                "judge.auto_review.problem_context.problem_data_storage", storage
            ):
                before = hash_current_problem(problem)
                storage.delete(name)
                storage.save(
                    name,
                    ContentFile(
                        b"signature_grader: {}\ntest_cases: []\npretest_test_cases:\n  - in: a.in\n"
                    ),
                )
                self.assertEqual(before, hash_current_problem(problem))

    def test_transition_preserves_current_signature_review_with_pretests(self):
        problem = self.make_problem("signaturemigration")
        problem.refresh_from_db()
        data = ProblemData.objects.get(problem=problem)
        data.use_ioi_signature = True
        data.save()
        config = {
            "signature_grader": {},
            "test_cases": [],
            "pretest_test_cases": [{"in": "a.in"}],
        }
        with tempfile.TemporaryDirectory() as root:
            storage = FileSystemStorage(location=root)
            storage.save(
                "signaturemigration/init.yml",
                ContentFile(
                    b"signature_grader: {}\ntest_cases: []\npretest_test_cases:\n  - in: a.in\n"
                ),
            )
            with patch(
                "judge.auto_review.problem_context.problem_data_storage", storage
            ), patch(
                "judge.management.commands.transition_review_hashes.problem_data_storage",
                storage,
            ):
                context = _capture_problem(
                    problem, extract_pdf=False, identity_only=True
                )
                context["signature_init_identity"] = {
                    "configuration_sha256": hash_transition.digest_json(
                        relevant_signature_config(
                            config,
                            context["allowed_language_keys"],
                            include_pretests=True,
                        )
                    )
                }
                old = hash_transition._old_problem_review_input_hash(context)
                run = ProblemReviewRun.objects.create(
                    problem=problem, status="D", input_hash=old
                )
                call_command(
                    "transition_review_hashes", apply=True, stdout=io.StringIO()
                )
                run.refresh_from_db()
                self.assertEqual(run.input_hash, hash_current_problem(problem))
