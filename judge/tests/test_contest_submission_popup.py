"""Regression test for the contest ranking submission popup link gating.

templates/submission/user-ajax.html (rendered by UserContestSubmissionsAjax) must gate
the per-submission link by the VIEWER (request.profile), not the submission owner. A
participant viewing another participant's popup must NOT get a link they can't open;
a superuser must.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

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
)


class ContestSubmissionPopupTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.lang = Language.objects.first()
        self.group = ProblemGroup.objects.create(name="g", full_name="Group")

        self.owner = self._profile("ctowner")  # target: has the submission
        self.other = self._profile("ctother")  # another participant (viewer)
        self.admin = self._profile("ctadmin", superuser=True)

        now = timezone.now()
        self.contest = Contest.objects.create(
            key="ctpop",
            name="CT Pop",
            start_time=now - timezone.timedelta(hours=2),
            end_time=now - timezone.timedelta(hours=1),  # ended
            is_visible=True,
        )
        self.problem = Problem.objects.create(
            code="ctp1",
            name="CTP1",
            description="d",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100.0,
            is_public=True,
        )
        self.cp = ContestProblem.objects.create(
            contest=self.contest, problem=self.problem, points=100, order=1
        )
        self.p_owner = ContestParticipation.objects.create(
            contest=self.contest, user=self.owner
        )
        self.p_other = ContestParticipation.objects.create(
            contest=self.contest, user=self.other
        )
        self.sub = Submission.objects.create(
            user=self.owner,
            problem=self.problem,
            language=self.lang,
            status="D",
            result="AC",
            points=100,
            case_points=100,
            case_total=100,
            time=0.1,
            memory=1024,
            contest_object=self.contest,
        )
        ContestSubmission.objects.create(
            submission=self.sub,
            problem=self.cp,
            participation=self.p_owner,
            points=100,
        )

    def _profile(self, name, superuser=False):
        user = User.objects.create_user(name, f"{name}@x.com", "pw")
        if superuser:
            user.is_superuser = True
            user.is_staff = True
            user.save()
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.lang}
        )
        return profile

    def _url(self):
        return reverse(
            "contest_user_submissions_ajax",
            args=[self.contest.key, self.p_owner.id, self.problem.code],
        )

    def test_link_hidden_from_other_participant(self):
        # another participant can see the attempts list but NOT a link to the source
        self.client.force_login(self.other.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse("submission_status", args=[self.sub.id]))

    def test_link_shown_to_superuser(self):
        self.client.force_login(self.admin.user)
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("submission_status", args=[self.sub.id]))

    def test_hidden_result_masks_score_but_keeps_owner_link(self):
        self.cp.is_result_hidden = True
        self.cp.save(update_fields=["is_result_hidden"])

        self.client.force_login(self.owner.user)
        resp = self.client.get(self._url())

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "?")
        self.assertContains(resp, reverse("submission_status", args=[self.sub.id]))
        self.assertNotContains(resp, "AC")
        self.assertNotContains(resp, "100 / 100")
        self.assertNotContains(resp, "Results are hidden")

    def test_hidden_result_masks_score_and_hides_link_from_other_participant(self):
        self.cp.is_result_hidden = True
        self.cp.save(update_fields=["is_result_hidden"])

        self.client.force_login(self.other.user)
        resp = self.client.get(self._url())

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "?")
        self.assertNotContains(resp, reverse("submission_status", args=[self.sub.id]))
        self.assertNotContains(resp, "AC")
        self.assertNotContains(resp, "100 / 100")
        self.assertNotContains(resp, "Results are hidden")
