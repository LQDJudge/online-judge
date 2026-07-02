from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestProblemClarification,
    Language,
    Problem,
    ProblemGroup,
    Profile,
)


class ContestClarificationVisibilityTest(TestCase):
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
        cls.group, _ = ProblemGroup.objects.get_or_create(
            name="clarifications", defaults={"full_name": "Clarifications"}
        )

    def setUp(self):
        self.participant = self._profile("clar_participant")
        self.outsider = self._profile("clar_outsider")
        self.author = self._profile("clar_author")

        now = timezone.now()
        self.contest = Contest.objects.create(
            key="clarcontest",
            name="Clarification Contest",
            start_time=now - timezone.timedelta(minutes=10),
            end_time=now + timezone.timedelta(hours=1),
            is_visible=True,
            is_rated=True,
            use_clarifications=True,
        )
        self.contest.authors.add(self.author)

        self.problem = Problem.objects.create(
            code="clarproblem",
            name="Clarification Problem",
            description="Statement",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100,
            is_public=False,
        )
        self.contest_problem = ContestProblem.objects.create(
            contest=self.contest,
            problem=self.problem,
            points=100,
            order=0,
        )
        self.secret = "reserve problem announcement should stay private"
        ContestProblemClarification.objects.create(
            contest=self.contest,
            problem=self.contest_problem,
            description=self.secret,
        )
        self.participation = ContestParticipation.objects.create(
            contest=self.contest,
            user=self.participant,
            virtual=ContestParticipation.LIVE,
        )
        self.participant.current_contest = self.participation
        self.participant.save(update_fields=["current_contest"])

    def _profile(self, username):
        user = User.objects.create_user(username, f"{username}@example.com", "pw")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": self.language}
        )
        return profile

    def _problems_url(self):
        return reverse("contest_problems", args=[self.contest.key])

    def _ajax_url(self):
        return reverse("contest_clarification_ajax", args=[self.contest.key])

    def test_problems_tab_hides_clarifications_from_non_participant(self):
        self.client.force_login(self.outsider.user)

        response = self.client.get(self._problems_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.secret)
        self.assertNotContains(response, "/clarification/ajax")

    def test_ajax_hides_clarifications_from_non_participant(self):
        self.client.force_login(self.outsider.user)

        response = self.client.get(self._ajax_url())

        self.assertEqual(response.status_code, 403)
        self.assertNotIn(self.secret, response.content.decode())

    def test_active_participant_can_see_clarifications(self):
        self.client.force_login(self.participant.user)

        problems_response = self.client.get(self._problems_url())
        ajax_response = self.client.get(self._ajax_url())

        self.assertContains(problems_response, self.secret)
        self.assertContains(problems_response, "/clarification/ajax")
        self.assertContains(ajax_response, self.secret)

    def test_contest_author_can_see_clarifications_without_joining(self):
        self.client.force_login(self.author.user)

        problems_response = self.client.get(self._problems_url())
        ajax_response = self.client.get(self._ajax_url())

        self.assertContains(problems_response, self.secret)
        self.assertContains(problems_response, "/clarification/ajax")
        self.assertContains(ajax_response, self.secret)
