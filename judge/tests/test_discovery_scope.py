"""Discovery stays relevant without changing superusers' direct access."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, Permission, User
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    Course,
    CourseContest,
    CourseRole,
    Language,
    Organization,
    Problem,
    Profile,
    Quiz,
    QuizQuestion,
    QuizQuestionAssignment,
)
from judge.utils.feed.pools import ProblemPool
from judge.views.contests import ContestListMixin
from judge.views.organization import OrganizationList
from judge.views.problem import ProblemList
from judge.views.quiz import QuestionBankList, QuizList


class DiscoveryScopeTests(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_superuser("discovery_admin", password="pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": Language.objects.first()}
        )
        self.organization = Organization.objects.create(
            name="Joined group",
            slug="discovery-group",
            short_name="DG",
            registrant=self.profile,
        )
        self.organization.members.add(self.profile)
        self.other_organization = Organization.objects.create(
            name="Other group",
            slug="discovery-other",
            short_name="DO",
            registrant=self.profile,
        )

    def request(self, query=None, anonymous=False):
        request = RequestFactory().get("/", query or {})
        request.user = AnonymousUser() if anonymous else self.user
        request.profile = None if anonymous else self.profile
        request.organization = None
        request.in_contest = False
        request.session = {}
        request.LANGUAGE_CODE = "en"
        return request

    def problem_ids(self, **kwargs):
        view = ProblemList()
        request = self.request(**kwargs)
        view.setup(request)
        view.setup_problem_list(request)
        return set(view.get_normal_queryset())

    def problem(self, code, **kwargs):
        return Problem.objects.create(
            code=code, name=code, time_limit=1, memory_limit=65536, points=100, **kwargs
        )

    def contest(self, key, **kwargs):
        now = timezone.now()
        return Contest.objects.create(
            key=key,
            name=key,
            start_time=now,
            end_time=now + timedelta(hours=1),
            **kwargs,
        )

    def test_problem_list_keeps_public_and_explicit_roles_but_not_unrelated_private(
        self,
    ):
        public = self.problem("scope-public", is_public=True)
        hidden = self.problem("scope-hidden", is_public=False)
        expected = {public.pk}
        for relation in ("authors", "curators", "testers"):
            problem = self.problem("scope-" + relation, is_public=False)
            getattr(problem, relation).add(self.profile)
            expected.add(problem.pk)
        self.assertSetEqual(self.problem_ids(), expected)
        self.assertTrue(hidden.is_accessible_by(self.user))
        self.assertIn(
            hidden.pk,
            Problem.get_visible_problems(self.user).values_list("pk", flat=True),
        )
        self.assertSetEqual(self.problem_ids(anonymous=True), {public.pk})

    def test_problem_list_respects_organization_membership(self):
        joined = self.problem("scope-org", is_public=True, is_organization_private=True)
        unrelated = self.problem(
            "scope-otherorg", is_public=True, is_organization_private=True
        )
        joined.organizations.add(self.organization)
        unrelated.organizations.add(self.other_organization)
        self.assertSetEqual(self.problem_ids(), {joined.pk})
        self.assertTrue(unrelated.is_accessible_by(self.user))

    def test_explicit_global_problem_permissions_do_not_expand_discovery(self):
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        self.user.user_permissions.add(
            *Permission.objects.filter(
                codename__in=[
                    "see_private_problem",
                    "edit_all_problem",
                    "see_organization_problem",
                ]
            )
        )
        hidden = self.problem("scope-privileged", is_public=False)
        self.assertFalse(self.problem_ids())
        self.assertIn(
            hidden.pk,
            Problem.get_visible_problems(self.user).values_list("pk", flat=True),
        )

    def test_contest_list_keeps_role_membership_and_course_visibility(self):
        public = self.contest("scope-public", is_visible=True)
        unrelated = self.contest("scope-hidden", is_visible=False)
        expected = {public.pk}
        for relation in ("authors", "curators", "testers"):
            contest = self.contest("scope-" + relation, is_visible=False)
            getattr(contest, relation).add(self.profile)
            expected.add(contest.pk)
        invited = self.contest("scope-invited", is_visible=True, is_private=True)
        invited.private_contestants.add(self.profile)
        org_contest = self.contest(
            "scope-org", is_visible=True, is_organization_private=True
        )
        org_contest.organizations.add(self.organization)
        other_org = self.contest(
            "scope-otherorg", is_visible=True, is_organization_private=True
        )
        other_org.organizations.add(self.other_organization)
        course = Course.objects.create(name="Enrolled", slug="scope-course", about="")
        CourseRole.objects.create(course=course, user=self.profile, role="ST")
        course_contest = self.contest(
            "scope-course", is_visible=True, is_in_course=True
        )
        CourseContest.objects.create(course=course, contest=course_contest, points=0)
        expected.update([invited.pk, org_contest.pk, course_contest.pk])
        view = ContestListMixin()
        view.request = self.request()
        self.assertSetEqual(
            set(view.get_queryset().values_list("pk", flat=True)), expected
        )
        self.assertTrue(unrelated.is_accessible_by(self.user))
        self.assertTrue(other_org.is_accessible_by(self.user))
        view.request = self.request(anonymous=True)
        self.assertSetEqual(
            set(view.get_queryset().values_list("pk", flat=True)), {public.pk}
        )

    def test_quiz_list_keeps_public_and_explicit_roles(self):
        public = Quiz.objects.create(
            code="scope-public", title="Public", is_public=True
        )
        hidden = Quiz.objects.create(
            code="scope-hidden", title="Hidden", is_public=False
        )
        expected = {public.pk}
        for relation in ("authors", "curators", "testers"):
            quiz = Quiz.objects.create(
                code="scope-" + relation, title=relation, is_public=False
            )
            getattr(quiz, relation).add(self.profile)
            expected.add(quiz.pk)
        view = QuizList()
        view.setup(self.request())
        self.assertSetEqual(
            set(view.get_queryset().values_list("pk", flat=True)), expected
        )
        self.assertTrue(hidden.is_accessible_by(self.user))
        view.setup(self.request({"search": "scope-hidden"}))
        self.assertFalse(view.get_queryset().exists())
        view.setup(self.request(anonymous=True))
        self.assertSetEqual(
            set(view.get_queryset().values_list("pk", flat=True)), {public.pk}
        )

    def test_question_bank_keeps_public_authored_and_curated_questions(self):
        public = QuizQuestion.objects.create(
            title="Public", question_type="MC", is_public=True
        )
        hidden = QuizQuestion.objects.create(
            title="Hidden", question_type="MC", is_public=False
        )
        expected = {public.pk}
        for relation in ("authors", "curators"):
            question = QuizQuestion.objects.create(
                title=relation, question_type="MC", is_public=False
            )
            getattr(question, relation).add(self.profile)
            expected.add(question.pk)
        view = QuestionBankList()
        view.setup(self.request())
        self.assertSetEqual(
            set(view.get_queryset().values_list("pk", flat=True)), expected
        )
        self.assertTrue(hidden.is_editable_by(self.user))
        view.setup(self.request({"public": "false"}))
        self.assertSetEqual(
            set(view.get_queryset().values_list("pk", flat=True)),
            expected - {public.pk},
        )

    def test_organization_tabs_do_not_expand_for_superusers(self):
        for tab in ("mine", "community", "public", "private", "blocked"):
            results = []
            for superuser in (True, False):
                self.user.is_superuser = superuser
                view = OrganizationList()
                view.setup(self.request())
                view.current_tab = tab
                view.organization_query = ""
                view.order = "name"
                results.append(set(view.get_queryset().values_list("pk", flat=True)))
            with self.subTest(tab=tab):
                self.assertSetEqual(*results)
                if tab == "mine":
                    self.assertSetEqual(results[0], {self.organization.pk})

    def test_superuser_can_still_open_unlisted_private_objects_directly(self):
        problem = self.problem("scope-direct-problem", is_public=False)
        contest = self.contest("scope_direct_contest", is_visible=False)
        quiz = Quiz.objects.create(
            code="scope-direct-quiz", title="Private", is_public=False
        )
        question = QuizQuestion.objects.create(
            title="Private", question_type="MC", is_public=False
        )
        self.client.force_login(self.user)
        for name, key in (
            ("problem_detail", problem.code),
            ("contest_view", contest.key),
            ("quiz_detail", quiz.code),
            ("question_bank_detail", question.pk),
        ):
            with self.subTest(route=name):
                self.assertEqual(
                    self.client.get(reverse(name, args=[key])).status_code, 200
                )

    def test_contest_mode_keeps_assigned_quizzes_and_question_scope(self):
        contest = self.contest("scope-active", is_visible=True)
        assigned = Quiz.objects.create(
            code="scope-assigned", title="Assigned", is_public=False
        )
        unrelated = Quiz.objects.create(
            code="scope-unrelated", title="Unrelated", is_public=False
        )
        unrelated.authors.add(self.profile)
        ContestProblem.objects.create(
            contest=contest, quiz=assigned, order=0, points=100
        )
        question = QuizQuestion.objects.create(
            title="Assigned", question_type="MC", is_public=True
        )
        hidden = QuizQuestion.objects.create(
            title="Private assigned", question_type="MC", is_public=False
        )
        for order, item in enumerate([question, hidden]):
            QuizQuestionAssignment.objects.create(
                quiz=assigned, question=item, order=order, points=1
            )
        participation = ContestParticipation.objects.create(
            contest=contest,
            user=self.profile,
            virtual=ContestParticipation.LIVE,
            real_start=timezone.now(),
        )
        self.profile.current_contest = participation
        request = self.request()
        request.in_contest = True
        for view_class, expected in [
            (QuizList, {assigned.pk}),
            (QuestionBankList, {question.pk}),
        ]:
            with self.subTest(view=view_class.__name__):
                view = view_class()
                view.setup(request)
                self.assertSetEqual(
                    set(view.get_queryset().values_list("pk", flat=True)), expected
                )
        self.assertTrue(hidden.is_editable_by(self.user))

    @override_settings(USE_ML=True)
    def test_feed_recommendations_use_ordinary_discovery_candidates(self):
        public = self.problem("scope-feed-public", is_public=True)
        self.problem("scope-feed-hidden", is_public=False)
        with patch(
            "judge.utils.problems.get_user_recommended_problems", return_value=[]
        ) as recommend:
            self.assertEqual(ProblemPool(self.request())._fetch(), [])
            recommend.assert_called_once()
            self.assertSetEqual(set(recommend.call_args.args[1]), {public.pk})
