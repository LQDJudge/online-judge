from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from judge import judgeapi
from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
    SubmissionSource,
)


class JudgeSubmissionPriorityTest(TestCase):
    """Verifies the priority tier assigned in judgeapi.judge_submission()."""

    OFFICIAL = 0
    PRIVATE = 1
    DEFAULT = 2
    REJUDGE = 3
    BATCH_REJUDGE = 4

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

    def setUp(self):
        self.user = User.objects.create_user(username="prio_user", password="pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.problem = Problem.objects.create(
            code="prio_prob",
            name="Priority Problem",
            group=self.problem_group,
            time_limit=1.0,
            memory_limit=65536,
            points=10,
        )

    def _make_contest(self, key, *, is_private=False, is_organization_private=False):
        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=key,
            start_time=now - timezone.timedelta(hours=1),
            end_time=now + timezone.timedelta(hours=1),
            is_private=is_private,
            is_organization_private=is_organization_private,
            is_visible=True,
            is_in_course=False,
        )

    def _make_submission(self, contest=None, virtual=ContestParticipation.LIVE):
        sub = Submission.objects.create(
            user=self.profile,
            problem=self.problem,
            language=self.language,
            status="QU",
        )
        SubmissionSource.objects.create(submission=sub, source="print('hi')")
        if contest is not None:
            cp, _ = ContestProblem.objects.get_or_create(
                problem=self.problem,
                contest=contest,
                defaults={"points": 100, "order": 1},
            )
            participation = ContestParticipation.objects.create(
                contest=contest, user=self.profile, virtual=virtual
            )
            ContestSubmission.objects.create(
                submission=sub, problem=cp, participation=participation
            )
        return sub

    def _priority_for(self, submission, **kwargs):
        captured = {}

        def fake_request(packet, reply=True):
            captured.update(packet)
            return {
                "name": "submission-received",
                "submission-id": packet["submission-id"],
            }

        with patch.object(judgeapi, "judge_request", side_effect=fake_request):
            judgeapi.judge_submission(submission, **kwargs)
        return captured["priority"]

    def test_non_contest_is_default(self):
        sub = self._make_submission()
        self.assertEqual(self._priority_for(sub), self.DEFAULT)

    def test_public_live_is_official(self):
        contest = self._make_contest("pub_live")
        sub = self._make_submission(contest=contest)
        self.assertEqual(self._priority_for(sub), self.OFFICIAL)

    def test_private_contest_is_private_tier(self):
        contest = self._make_contest("priv", is_private=True)
        sub = self._make_submission(contest=contest)
        self.assertEqual(self._priority_for(sub), self.PRIVATE)

    def test_org_private_contest_is_private_tier(self):
        contest = self._make_contest("org_priv", is_organization_private=True)
        sub = self._make_submission(contest=contest)
        self.assertEqual(self._priority_for(sub), self.PRIVATE)

    def test_public_virtual_falls_back_to_default(self):
        contest = self._make_contest("pub_virt")
        sub = self._make_submission(contest=contest, virtual=1)
        self.assertEqual(self._priority_for(sub), self.DEFAULT)

    def test_private_virtual_falls_back_to_default(self):
        contest = self._make_contest("priv_virt", is_private=True)
        sub = self._make_submission(contest=contest, virtual=2)
        self.assertEqual(self._priority_for(sub), self.DEFAULT)

    def test_rejudge_flag_overrides_contest(self):
        contest = self._make_contest("pub_rejudge")
        sub = self._make_submission(contest=contest)
        self.assertEqual(self._priority_for(sub, rejudge=True), self.REJUDGE)

    def test_batch_rejudge_flag_overrides_contest(self):
        contest = self._make_contest("pub_batch")
        sub = self._make_submission(contest=contest)
        self.assertEqual(
            self._priority_for(sub, batch_rejudge=True), self.BATCH_REJUDGE
        )
