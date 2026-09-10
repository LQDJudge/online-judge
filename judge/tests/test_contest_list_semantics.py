from datetime import timedelta
import re
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    BestSubmission,
    Contest,
    ContestParticipation,
    ContestProblem,
    Course,
    Language,
    OfficialContest,
    OfficialContestCategory,
    OfficialContestLocation,
    Problem,
    ProblemGroup,
    Profile,
    Organization,
    Submission,
)
from judge.models.course import CourseContest, CourseRole, RoleInCourse
from judge.views.contests import ContestList


@override_settings(LANGUAGE_CODE="en")
class ContestListSemanticsTest(TestCase):
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
        user = User.objects.create_user("contest-list-user", password="pw")
        cls.profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": cls.language}
        )
        now = timezone.now()

        cls.active_contest = Contest.objects.create(
            key="listactive",
            name="Active contest",
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=2),
            is_visible=True,
        )
        cls.active_participation = ContestParticipation.objects.create(
            contest=cls.active_contest,
            user=cls.profile,
            virtual=ContestParticipation.LIVE,
            real_start=now - timedelta(minutes=30),
        )
        cls.profile.current_contest = cls.active_participation
        cls.profile.save(update_fields=["current_contest"])

        cls.official_live_contest = Contest.objects.create(
            key="listofficiallive",
            name="Official live contest",
            start_time=now - timedelta(minutes=30),
            end_time=now + timedelta(hours=3),
            is_visible=True,
        )
        cls.regular_past_contest = Contest.objects.create(
            key="listregularpast",
            name="Regular past contest",
            start_time=now - timedelta(days=2),
            end_time=now - timedelta(days=1),
            is_visible=True,
        )
        cls.official_past_contest = Contest.objects.create(
            key="listofficialpast",
            name="Official past contest",
            start_time=now - timedelta(days=3),
            end_time=now - timedelta(days=2),
            is_visible=True,
        )
        cls.upcoming_contest = Contest.objects.create(
            key="listupcoming",
            name="Upcoming contest",
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=1, hours=2),
            is_visible=True,
        )

        cls.category = OfficialContestCategory.objects.create(name="Test series")
        location = OfficialContestLocation.objects.create(name="Test location")
        OfficialContest.objects.create(
            contest=cls.official_live_contest,
            category=cls.category,
            location=location,
            year=2026,
        )
        OfficialContest.objects.create(
            contest=cls.official_past_contest,
            category=cls.category,
            location=location,
            year=2025,
        )

    def setUp(self):
        self.client.force_login(self.profile.user)

    def test_active_is_personal_subset_of_complete_ongoing_list(self):
        response = self.client.get(reverse("contest_list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context_data["current_tab"], "active")
        self.assertEqual(
            [item.contest_id for item in response.context_data["contests"]],
            [self.active_contest.id],
        )
        self.assertContains(response, "Continue")

        response = self.client.get(reverse("contest_list"), {"tab": "current"})
        self.assertEqual(response.context_data["current_tab"], "current")
        contest_ids = {contest.id for contest in response.context_data["contests"]}
        self.assertSetEqual(
            contest_ids,
            {self.active_contest.id, self.official_live_contest.id},
        )
        self.assertEqual(response.context_data["current_count"], 2)
        self.assertContains(response, "Continue")
        self.assertContains(response, "Official")

    def test_general_past_excludes_official_archive_contests(self):
        response = self.client.get(reverse("contest_list"), {"tab": "past"})
        contest_ids = {contest.id for contest in response.context_data["contests"]}
        self.assertIn(self.regular_past_contest.id, contest_ids)
        self.assertNotIn(self.official_past_contest.id, contest_ids)

    @override_settings(USE_ML=False)
    def test_list_join_forms_pass_csrf_validation(self):
        cases = (
            ("recommended_contest_list", {}, self.regular_past_contest),
            (
                "official_contest_list",
                {"category": self.category.id},
                self.official_past_contest,
            ),
            (
                "official_contest_list",
                {"category": self.category.id},
                self.official_live_contest,
            ),
            ("contest_list", {"tab": "past"}, self.regular_past_contest),
        )
        for route, params, contest in cases:
            with self.subTest(route=route, contest=contest.key):
                client = Client(enforce_csrf_checks=True)
                client.force_login(self.profile.user)
                response = client.get(reverse(route), params)
                self.assertEqual(response.status_code, 200)
                join_url = reverse("contest_join", args=[contest.key])
                form = re.search(
                    r'<form action="' + re.escape(join_url) + r'"[^>]*>(.*?)</form>',
                    response.content.decode(),
                    re.DOTALL,
                )
                self.assertIsNotNone(form)
                token = re.search(
                    r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)['\"]",
                    form.group(1),
                )
                self.assertIsNotNone(token, "Join form must contain its CSRF token")

                response = client.post(
                    join_url, {"csrfmiddlewaretoken": token.group(1)}
                )

                self.assertRedirects(
                    response,
                    reverse("contest_problems", args=[contest.key]),
                    fetch_redirect_response=False,
                )
                self.profile.refresh_from_db()
                participation = self.profile.current_contest
                self.assertEqual(participation.contest_id, contest.id)
                self.assertEqual(participation.virtual > 0, contest.ended)
                self.assertFalse(participation.ended)
                self.profile.remove_contest()

    def test_upcoming_contest_has_view_action(self):
        response = self.client.get(reverse("contest_list"), {"tab": "future"})
        self.assertEqual(response.context_data["current_tab"], "future")
        self.assertContains(response, "Upcoming contest")
        self.assertContains(response, ">View</a>", html=False)

    @override_settings(USE_ML=False)
    def test_recommendations_reuse_cards_with_status_aware_actions(self):
        response = self.client.get(reverse("recommended_contest_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="list-contest"')
        self.assertContains(response, "contest-card-main")
        self.assertContains(response, "Official")
        html = response.content.decode()

        def contest_card(contest):
            heading = html.index(f'id="contest-card-{contest.key}"')
            start = html.rindex("<article", 0, heading)
            end = html.index("</article>", heading)
            return html[start:end]

        live_card = contest_card(self.official_live_contest)
        self.assertIn('value="Join"', live_card)
        self.assertNotIn('value="Virtual join"', live_card)

        upcoming_card = contest_card(self.upcoming_contest)
        self.assertIn(">View</a>", upcoming_card)
        self.assertNotIn('value="Virtual join"', upcoming_card)

        past_card = contest_card(self.regular_past_contest)
        self.assertIn('value="Virtual join"', past_card)

    def test_explicit_filter_submission_can_clear_session_checkbox(self):
        session = self.client.session
        session["show_only_rated_contests"] = True
        session.save()

        self.client.get(
            reverse("contest_list"),
            {"tab": "current", "filters": "1"},
        )
        self.assertFalse(self.client.session["show_only_rated_contests"])

    def test_official_landing_skips_contest_page_and_filter_loads_results(self):
        response = self.client.get(reverse("official_contest_list"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context_data["show_collection_browser"])
        self.assertEqual(list(response.context_data["contests"]), [])
        self.assertContains(response, "Test series")

        response = self.client.get(
            reverse("official_contest_list"),
            {"category": self.category.id},
        )
        self.assertFalse(response.context_data["show_collection_browser"])
        contest_ids = {contest.id for contest in response.context_data["contests"]}
        self.assertSetEqual(
            contest_ids,
            {self.official_live_contest.id, self.official_past_contest.id},
        )

    def test_best_submission_progress_is_only_shown_for_past_contests(self):
        group = ProblemGroup.objects.create(
            name="list-progress", full_name="Contest list progress"
        )
        problem = Problem.objects.create(
            code="listprogress",
            name="List progress problem",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=10,
        )
        for contest in (
            self.regular_past_contest,
            self.official_past_contest,
            self.official_live_contest,
            self.upcoming_contest,
        ):
            ContestProblem.objects.create(
                contest=contest,
                problem=problem,
                points=100,
                partial=True,
                order=0,
            )
        hidden_past_contest = Contest.objects.create(
            key="listhiddenpast",
            name="Hidden-result past contest",
            start_time=timezone.now() - timedelta(days=4),
            end_time=timezone.now() - timedelta(days=3),
            is_visible=True,
        )
        ContestProblem.objects.create(
            contest=hidden_past_contest,
            problem=problem,
            points=100,
            partial=True,
            order=0,
            is_result_hidden=True,
        )

        submission = Submission.objects.create(
            user=self.profile,
            problem=problem,
            language=self.language,
            status="D",
            points=4,
            case_points=40,
            case_total=100,
        )
        BestSubmission.objects.create(
            user=self.profile,
            problem=problem,
            submission=submission,
            points=40,
            case_total=100,
        )

        view = ContestList()
        view.request = SimpleNamespace(
            profile=self.profile,
            user=self.profile.user,
        )
        with self.assertNumQueries(2):
            progress = view._get_past_contest_progress(
                [
                    self.regular_past_contest,
                    self.official_past_contest,
                    self.official_live_contest,
                    self.upcoming_contest,
                    hidden_past_contest,
                ]
            )
        self.assertSetEqual(
            set(progress),
            {self.regular_past_contest.id, self.official_past_contest.id},
        )
        self.assertNotIn(hidden_past_contest.id, progress)

        past_response = self.client.get(reverse("contest_list"), {"tab": "past"})
        self.assertEqual(
            past_response.context_data["contest_progress"][
                self.regular_past_contest.id
            ],
            {"achieved": 40.0, "total": 100.0},
        )
        self.assertContains(past_response, "Progress:")
        self.assertContains(past_response, "40 / 100")
        self.assertNotIn(
            hidden_past_contest.id,
            past_response.context_data["contest_progress"],
        )

        current_response = self.client.get(reverse("contest_list"), {"tab": "current"})
        self.assertNotIn(
            self.official_live_contest.id,
            current_response.context_data["contest_progress"],
        )
        self.assertNotContains(current_response, "Progress:")

        future_response = self.client.get(reverse("contest_list"), {"tab": "future"})
        self.assertNotIn(
            self.upcoming_contest.id,
            future_response.context_data["contest_progress"],
        )
        self.assertNotContains(future_response, "Progress:")

        official_response = self.client.get(
            reverse("official_contest_list"), {"category": self.category.id}
        )
        self.assertEqual(
            official_response.context_data["contest_progress"][
                self.official_past_contest.id
            ],
            {"achieved": 40.0, "total": 100.0},
        )
        self.assertNotIn(
            self.official_live_contest.id,
            official_response.context_data["contest_progress"],
        )
        self.assertContains(official_response, "Progress:", count=1)


class ContestVisibilityQueryTest(TestCase):
    fixtures = ["language_small"]

    def setUp(self):
        self.user = User.objects.create_user("visible-contest-user", password="pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": Language.objects.first()}
        )
        self.other = User.objects.create_user("visible-contest-other", password="pw")
        self.other_profile, _ = Profile.objects.get_or_create(
            user=self.other, defaults={"language": Language.objects.first()}
        )
        now = timezone.now()
        self.contest_defaults = {
            "start_time": now - timedelta(hours=1),
            "end_time": now + timedelta(hours=1),
            "is_visible": True,
        }

    def make_contest(self, key, **kwargs):
        defaults = dict(self.contest_defaults)
        defaults.update(kwargs)
        return Contest.objects.create(key=key, name=key, **defaults)

    def visible_ids(self, user, show_own=False):
        return set(
            Contest.get_visible_contests(user, show_own).values_list("id", flat=True)
        )

    def test_public_private_and_scoreboard_visibility(self):
        public = self.make_contest("exists-public")
        private = self.make_contest("exists-private", is_private=True)
        scoreboard = self.make_contest("exists-scoreboard", is_private=True)
        private.private_contestants.add(self.profile)
        scoreboard.view_contest_scoreboard.add(self.profile)

        self.assertSetEqual(
            self.visible_ids(self.user), {public.id, private.id, scoreboard.id}
        )
        self.assertSetEqual(self.visible_ids(self.other), {public.id})

    def test_organization_and_course_visibility(self):
        organization = Organization.objects.create(
            name="Visibility Org",
            slug="visibility-org",
            short_name="VO",
            registrant=self.profile,
        )
        organization.members.add(self.profile)
        organization_contest = self.make_contest(
            "exists-org", is_organization_private=True
        )
        organization_contest.organizations.add(organization)

        course = Course.objects.create(
            name="Visibility Course", slug="visibility-course", about=""
        )
        CourseRole.objects.create(
            course=course, user=self.profile, role=RoleInCourse.STUDENT
        )
        course_contest = self.make_contest("exists-course", is_in_course=True)
        CourseContest.objects.create(course=course, contest=course_contest, points=0)

        self.assertSetEqual(
            self.visible_ids(self.user),
            {organization_contest.id, course_contest.id},
        )
        self.assertFalse(self.visible_ids(self.other))

    def test_editors_see_invisible_contests(self):
        authored = self.make_contest("exists-authored", is_visible=False)
        curated = self.make_contest("exists-curated", is_visible=False)
        tested = self.make_contest("exists-tested", is_visible=False)
        authored.authors.add(self.profile)
        curated.curators.add(self.profile)
        tested.testers.add(self.profile)

        self.assertSetEqual(
            self.visible_ids(self.user), {authored.id, curated.id, tested.id}
        )

    def test_superuser_show_own_uses_normal_visibility_rules(self):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save(update_fields=["is_superuser", "is_staff"])
        public = self.make_contest("exists-super-public")
        inaccessible = self.make_contest(
            "exists-super-inaccessible", is_visible=False, is_private=True
        )

        self.assertIn(inaccessible.id, self.visible_ids(self.user))
        self.assertSetEqual(self.visible_ids(self.user, show_own=True), {public.id})

    def test_privileged_user_without_profile_uses_fast_path(self):
        privileged = User.objects.create_superuser(
            "profileless-contest-admin", password="pw"
        )
        public = self.make_contest("exists-profileless-admin")
        self.assertFalse(Profile.objects.filter(user=privileged).exists())

        self.assertIn(public.id, self.visible_ids(privileged))
