from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Judge,
    Language,
    Problem,
    ProblemGroup,
    Profile,
    Submission,
)


@override_settings(USE_ML=False)
class ContestSpectatorSubmitTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.language = Language.objects.get(key="PY3")
        self.group = ProblemGroup.objects.create(
            name="spectator", full_name="Spectator"
        )

        now = timezone.now()
        self.contest = Contest.objects.create(
            key="spectatebug",
            name="Spectate Bug",
            start_time=now - timezone.timedelta(hours=1),
            end_time=now + timezone.timedelta(hours=1),
            time_limit=timezone.timedelta(minutes=30),
            is_visible=True,
        )
        self.problem = Problem.objects.create(
            code="spectateprob",
            name="Spectate Problem",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100,
            is_public=False,
        )
        self.problem.allowed_languages.add(self.language)
        self.contest_problem = ContestProblem.objects.create(
            contest=self.contest,
            problem=self.problem,
            points=100,
            order=0,
            max_submissions=1,
        )

        self.judge = Judge.objects.create(
            name="spectator-judge",
            auth_key="key",
            online=True,
            start_time=now,
            ping=0,
            load=0,
        )
        self.judge.runtimes.add(self.language)
        self.judge.problems.add(self.problem)

    def _profile(self, username):
        user = User.objects.create_user(username, password="pw")
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.language = self.language
        profile.save(update_fields=["language"])
        return profile

    def _set_current_participation(self, profile, virtual, real_start=None):
        participation = ContestParticipation.objects.create(
            contest=self.contest,
            user=profile,
            virtual=virtual,
            real_start=real_start or timezone.now() - timezone.timedelta(hours=1),
        )
        profile.current_contest = participation
        profile.save(update_fields=["current_contest"])
        return participation

    def _submit(self, profile):
        self.client.force_login(profile.user)
        return self.client.post(
            reverse("problem_submit", args=[self.problem.code]),
            {
                "language": self.language.id,
                "source": "print(1)",
            },
        )

    def test_unprivileged_spectator_cannot_submit_contest_problem(self):
        profile = self._profile("ended_participant")
        self._set_current_participation(profile, ContestParticipation.SPECTATE)

        response = self._submit(profile)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Submission.objects.exists())
        self.assertFalse(ContestSubmission.objects.exists())

    def test_tester_spectator_can_submit_for_testing(self):
        profile = self._profile("contest_tester")
        self.contest.testers.add(profile)
        participation = self._set_current_participation(
            profile, ContestParticipation.SPECTATE
        )

        with patch.object(Submission, "judge"):
            response = self._submit(profile)

        self.assertEqual(response.status_code, 302)
        contest_submission = ContestSubmission.objects.get()
        self.assertEqual(contest_submission.participation, participation)

    def test_editor_spectator_can_submit_for_testing(self):
        profile = self._profile("contest_editor")
        self.contest.authors.add(profile)
        participation = self._set_current_participation(
            profile, ContestParticipation.SPECTATE
        )

        with patch.object(Submission, "judge"):
            response = self._submit(profile)

        self.assertEqual(response.status_code, 302)
        contest_submission = ContestSubmission.objects.get()
        self.assertEqual(contest_submission.participation, participation)

    def test_submit_page_disables_button_when_no_submissions_left(self):
        profile = self._profile("contest_live")
        participation = self._set_current_participation(
            profile, ContestParticipation.LIVE, real_start=timezone.now()
        )
        submission = Submission.objects.create(
            user=profile,
            problem=self.problem,
            language=self.language,
            contest_object=self.contest,
            status="D",
            result="AC",
            points=100,
            case_points=100,
            case_total=100,
            time=0.1,
            memory=1024,
        )
        ContestSubmission.objects.create(
            submission=submission,
            problem=self.contest_problem,
            participation=participation,
            points=100,
        )

        self.client.force_login(profile.user)
        response = self.client.get(reverse("problem_submit", args=[self.problem.code]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="submit-button"', content)
        self.assertRegex(content, r'id="submit-button"[^>]*disabled')

    def test_problem_page_disables_submit_button_for_unprivileged_spectator(self):
        profile = self._profile("problem_page_spectator")
        self._set_current_participation(profile, ContestParticipation.SPECTATE)

        self.client.force_login(profile.user)
        response = self.client.get(reverse("problem_detail", args=[self.problem.code]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "btn-disabled")
        self.assertNotContains(
            response, reverse("problem_submit", args=[self.problem.code])
        )
