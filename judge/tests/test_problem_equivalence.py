from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from judge.models import (
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
    SubmissionSource,
)
from judge.utils.problem_equivalence import (
    ProblemEquivalenceError,
    ProblemEquivalenceVerifier,
)


class ProblemEquivalenceVerifierTestCase(TestCase):
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
            name="equivalence", defaults={"full_name": "Equivalence Tests"}
        )

    def setUp(self):
        self.user = User.objects.create_user("equivalence_user", password="password")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.source = self.make_problem("equivsource")
        self.target = self.make_problem("equivtarget")

    def make_problem(self, code, **kwargs):
        defaults = {
            "code": code,
            "name": f"Problem {code}",
            "description": "same statement",
            "group": self.problem_group,
            "time_limit": 1.0,
            "memory_limit": 65536,
            "points": 100.0,
            "is_public": True,
        }
        defaults.update(kwargs)
        return Problem.objects.create(**defaults)

    def make_submission(
        self,
        problem,
        *,
        source="print(42)",
        points=100,
        result="AC",
        status="D",
    ):
        submission = Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status=status,
            result=result,
            points=points,
            case_points=points,
            case_total=100,
            time=0.1,
            memory=1024,
        )
        SubmissionSource.objects.create(submission=submission, source=source)
        return submission

    def test_dry_run_selects_best_ac_submission_without_mutating(self):
        weak = self.make_submission(self.source, source="print('weak')", points=80)
        strong = self.make_submission(self.source, source="print('strong')", points=100)

        report = ProblemEquivalenceVerifier(
            self.source.code,
            self.target.code,
            apply=False,
        ).run()

        self.assertEqual(report["source"]["code"], self.source.code)
        self.assertEqual(report["target"]["code"], self.target.code)
        self.assertEqual(report["source_submission_id"], strong.id)
        self.assertEqual(Submission.objects.filter(problem=self.target).count(), 0)
        self.assertNotEqual(report["source_submission_id"], weak.id)

    def test_apply_clones_source_and_queues_judge(self):
        source_submission = self.make_submission(self.source, source="print('ok')")

        with patch.object(Submission, "judge", return_value=True) as judge_mock:
            report = ProblemEquivalenceVerifier(
                self.source.code,
                self.target.code,
                apply=True,
            ).run()

        clone = Submission.objects.get(id=report["verification_submission_id"])
        self.assertEqual(clone.problem, self.target)
        self.assertEqual(clone.user, source_submission.user)
        self.assertEqual(clone.language, source_submission.language)
        self.assertEqual(clone.source.source, "print('ok')")
        self.assertTrue(report["queued"])
        judge_mock.assert_called_once_with(rejudge=False, judge_id=None)

    def test_rejects_non_ac_explicit_submission(self):
        bad_submission = self.make_submission(self.source, result="WA", points=0)

        with self.assertRaises(ProblemEquivalenceError):
            ProblemEquivalenceVerifier(
                self.source.code,
                self.target.code,
                source_submission_id=bad_submission.id,
            ).run()

    def test_management_command_dry_run_outputs_json(self):
        self.make_submission(self.source)

        output = StringIO()
        call_command(
            "verify_problem_equivalence",
            "--source",
            self.source.code,
            "--target",
            self.target.code,
            stdout=output,
        )

        self.assertIn('"applied": false', output.getvalue())

    def test_management_command_both_requires_apply_for_queueing(self):
        self.make_submission(self.source)
        self.make_submission(self.target)

        with patch.object(Submission, "judge", return_value=True):
            call_command(
                "verify_problem_equivalence",
                "--source",
                self.source.code,
                "--target",
                self.target.code,
                "--both",
                "--apply",
                stdout=StringIO(),
            )

        self.assertEqual(Submission.objects.filter(problem=self.source).count(), 2)
        self.assertEqual(Submission.objects.filter(problem=self.target).count(), 2)

    def test_command_errors_when_no_ac_submission_exists(self):
        self.make_submission(self.source, result="WA", points=0)

        with self.assertRaises(CommandError):
            call_command(
                "verify_problem_equivalence",
                "--source",
                self.source.code,
                "--target",
                self.target.code,
            )
