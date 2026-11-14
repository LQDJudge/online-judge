"""Contest hash batching preserves per-problem identity and bounds DB queries."""

from datetime import timedelta
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
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
