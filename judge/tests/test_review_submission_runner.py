from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase

from judge.models import Language, Problem, ProblemGroup, Profile, Submission
from judge.review.submission_runner import JudgeTimeout, judge_and_wait


class JudgeAndWaitTest(TestCase):
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
            name="TG", defaults={"full_name": "Test Group"}
        )
        user = User.objects.create_user("jw", "jw@x.com", "pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        cls.problem = Problem.objects.create(
            code="jw1",
            name="JW",
            description="x" * 200,
            group=cls.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
            partial=False,
        )
        cls.problem.authors.add(cls.profile)

    @patch("judge.review.submission_runner._poll_submission")
    @patch("judge.review.submission_runner._create_submission")
    def test_returns_submission_when_judged(self, mock_create, mock_poll):
        sub = MagicMock(spec=Submission)
        sub.id = 42
        sub.status = "D"
        sub.result = "AC"
        mock_create.return_value = sub
        mock_poll.return_value = sub  # already done
        out = judge_and_wait(self.problem, self.language, "int main(){}", self.profile)
        self.assertEqual(out.id, 42)

    @patch("judge.review.submission_runner._poll_submission")
    @patch("judge.review.submission_runner._create_submission")
    def test_raises_on_timeout(self, mock_create, mock_poll):
        sub = MagicMock(spec=Submission)
        sub.id = 43
        sub.status = "QU"
        sub.result = None
        mock_create.return_value = sub
        mock_poll.side_effect = JudgeTimeout("timed out")
        with self.assertRaises(JudgeTimeout):
            judge_and_wait(self.problem, self.language, "src", self.profile)
