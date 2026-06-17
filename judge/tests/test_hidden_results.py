import csv
from io import StringIO

from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import (
    BestSubmission,
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
from judge.views.problem import ProblemList
from judge.views.submission import _get_global_submission_result_data


class HiddenContestResultTest(TestCase):
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
            name="hidden-results", defaults={"full_name": "Hidden Results"}
        )

    def setUp(self):
        self.user = User.objects.create_user("hidden_user", password="pw")
        self.profile, _ = Profile.objects.get_or_create(
            user=self.user, defaults={"language": self.language}
        )
        self.other_user = User.objects.create_user("hidden_other", password="pw")
        self.other_profile, _ = Profile.objects.get_or_create(
            user=self.other_user, defaults={"language": self.language}
        )

        now = timezone.now()
        self.contest = Contest.objects.create(
            key="hiddenres",
            name="Hidden Result Contest",
            start_time=now - timezone.timedelta(minutes=10),
            end_time=now + timezone.timedelta(hours=1),
            is_visible=True,
            is_rated=True,
        )
        self.problem = Problem.objects.create(
            code="hiddenprob",
            name="Hidden Problem",
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
            is_result_hidden=True,
        )
        self.normal_problem = Problem.objects.create(
            code="normalprob",
            name="Normal Problem",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100,
            is_public=True,
        )
        self.normal_contest_problem = ContestProblem.objects.create(
            contest=self.contest,
            problem=self.normal_problem,
            points=100,
            order=1,
            is_result_hidden=False,
        )
        self.participation = ContestParticipation.objects.create(
            contest=self.contest,
            user=self.profile,
            virtual=ContestParticipation.LIVE,
            score=0,
            format_data={str(self.contest_problem.id): {"points": 100, "time": 30}},
        )
        self.profile.current_contest = self.participation
        self.profile.save(update_fields=["current_contest"])
        self.other_participation = ContestParticipation.objects.create(
            contest=self.contest,
            user=self.other_profile,
            virtual=ContestParticipation.LIVE,
            score=0,
            format_data={str(self.contest_problem.id): {"points": 50, "time": 50}},
        )
        self.other_profile.current_contest = self.other_participation
        self.other_profile.save(update_fields=["current_contest"])

        self.submission = Submission.objects.create(
            user=self.profile,
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
            submission=self.submission,
            problem=self.contest_problem,
            participation=self.participation,
            points=100,
        )
        BestSubmission.objects.create(
            user=self.profile,
            problem=self.problem,
            submission=self.submission,
            points=100,
            case_total=100,
        )
        self.normal_submission = Submission.objects.create(
            user=self.profile,
            problem=self.normal_problem,
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
            submission=self.normal_submission,
            problem=self.normal_contest_problem,
            participation=self.participation,
            points=100,
        )

    def assert_submission_result_masked(self, response, submission=None):
        if submission is not None:
            self.assertContains(response, f'id="{submission.id}"')
        self.assertContains(response, '<div class="score">?</div>', html=True)
        self.assertContains(response, '<div class="time">---</div>', html=True)
        self.assertContains(response, '<div class="memory">---</div>', html=True)
        self.assertNotContains(response, "100 / 100")

    def test_contest_submission_inherits_hidden_result_flag(self):
        self.submission.contest.refresh_from_db()
        self.normal_submission.contest.refresh_from_db()

        self.assertTrue(self.submission.contest.is_result_hidden)
        self.assertFalse(self.normal_submission.contest.is_result_hidden)

    def test_contest_problem_hidden_result_toggle_syncs_contest_submissions(self):
        self.contest_problem.is_result_hidden = False
        self.contest_problem.save()
        self.submission.contest.refresh_from_db()
        self.assertFalse(self.submission.contest.is_result_hidden)

        self.contest_problem.is_result_hidden = True
        self.contest_problem.save()
        self.submission.contest.refresh_from_db()
        self.assertTrue(self.submission.contest.is_result_hidden)

    def test_contest_problem_unrelated_save_does_not_sync_hidden_result_flag(self):
        ContestSubmission.objects.filter(submission=self.submission).update(
            is_result_hidden=False
        )

        self.contest_problem.points = 99
        self.contest_problem.save()
        self.submission.contest.refresh_from_db()

        self.assertFalse(self.submission.contest.is_result_hidden)

    def test_own_problem_submissions_mask_hidden_result(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("user_submissions", args=[self.problem.code, self.user.username])
        )

        self.assertEqual(response.status_code, 200)
        self.assert_submission_result_masked(response, self.submission)

    def test_real_status_filter_excludes_hidden_result_submission(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("user_submissions", args=[self.problem.code, self.user.username]),
            {"status": "WA"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'id="{self.submission.id}"')

    def test_hidden_status_filter_shows_hidden_result_submission(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("user_submissions", args=[self.problem.code, self.user.username]),
            {"status": "HIDDEN"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="{self.submission.id}"')
        self.assert_submission_result_masked(response, self.submission)

    def test_real_status_filter_excludes_hidden_result_submission_for_ac_filter(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("user_submissions", args=[self.problem.code, self.user.username]),
            {"status": "AC"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'id="{self.submission.id}"')

    def test_submission_stats_show_hidden_bucket(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("user_submissions", args=[self.problem.code, self.user.username]),
            {"results": "1"},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        categories = {
            category["code"]: category["count"]
            for category in data["results_json"]["categories"]
        }
        self.assertEqual(categories["HIDDEN"], 1)
        self.assertEqual(categories["AC"], 0)
        self.assertEqual(data["results_json"]["total"], 1)
        self.assertIn("HIDDEN", data["results_colors_json"])

    def test_contest_submission_stats_keep_visible_problem_counts(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("contest_submissions", args=[self.contest.key]), {"results": "1"}
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        categories = {
            category["code"]: category["count"]
            for category in data["results_json"]["categories"]
        }
        self.assertEqual(categories["HIDDEN"], 1)
        self.assertEqual(categories["AC"], 1)
        self.assertEqual(data["results_json"]["total"], 2)

    def test_global_submission_stats_use_cached_hidden_bucket_path(self):
        _get_global_submission_result_data.dirty((), (), ())
        self.client.force_login(self.user)
        response = self.client.get(reverse("all_submissions"), {"results": "1"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        categories = {
            category["code"]: category["count"]
            for category in data["results_json"]["categories"]
        }
        self.assertEqual(categories["HIDDEN"], 1)
        self.assertEqual(categories["AC"], 1)
        self.assertEqual(data["results_json"]["total"], 2)

    def test_contest_editor_submission_stats_show_real_hidden_results(self):
        self.contest.authors.add(self.profile)
        self.contest._author_ids.dirty(self.contest)
        self.contest.__dict__.pop("author_ids", None)
        self.contest.__dict__.pop("editor_ids", None)
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("contest_submissions", args=[self.contest.key]), {"results": "1"}
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        categories = {
            category["code"]: category["count"]
            for category in data["results_json"]["categories"]
        }
        self.assertNotIn("HIDDEN", categories)
        self.assertEqual(categories["AC"], 2)
        self.assertEqual(data["results_json"]["total"], 2)

    def test_global_editor_submission_stats_show_real_hidden_results(self):
        self.contest.authors.add(self.profile)
        self.contest._author_ids.dirty(self.contest)
        editable_ids = (self.contest.id,)
        _get_global_submission_result_data.dirty((), (), editable_ids)
        self.client.force_login(self.user)

        response = self.client.get(reverse("all_submissions"), {"results": "1"})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        categories = {
            category["code"]: category["count"]
            for category in data["results_json"]["categories"]
        }
        self.assertNotIn("HIDDEN", categories)
        self.assertEqual(categories["AC"], 2)
        self.assertEqual(data["results_json"]["total"], 2)

    def test_other_user_hidden_result_submissions_are_masked(self):
        other_submission = Submission.objects.create(
            user=self.other_profile,
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
            submission=other_submission,
            problem=self.contest_problem,
            participation=self.other_participation,
            points=100,
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse(
                "contest_user_submissions",
                args=[self.contest.key, self.other_user.username, self.problem.code],
            ),
            {"status": "HIDDEN"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="{other_submission.id}"')
        self.assert_submission_result_masked(response, other_submission)

    def test_ranked_submissions_block_hidden_current_contest_problem(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("ranked_submissions", args=[self.problem.code])
        )

        self.assertEqual(response.status_code, 404)

    def test_ranked_submissions_keep_normal_problem_from_same_contest(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("ranked_submissions", args=[self.normal_problem.code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "100 / 100")

    def test_ranked_submission_stats_show_hidden_bucket(self):
        self.problem.is_public = True
        self.problem.save(update_fields=["is_public"])
        self.contest.end_time = timezone.now() - timezone.timedelta(minutes=1)
        self.contest.save(update_fields=["end_time"])
        self.profile.current_contest = None
        self.profile.save(update_fields=["current_contest"])
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("ranked_submissions", args=[self.problem.code]), {"results": "1"}
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        categories = {
            category["code"]: category["count"]
            for category in data["results_json"]["categories"]
        }
        self.assertEqual(categories["HIDDEN"], 1)
        self.assertEqual(categories["AC"], 0)
        self.assertEqual(data["results_json"]["total"], 1)

    def test_single_submission_widget_masks_hidden_result(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("submission_single_query"),
            {"id": self.submission.id, "show_problem": "0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assert_submission_result_masked(response)

    def test_problem_detail_hides_hidden_best_submission_state(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("problem_detail", args=[self.problem.code]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "solved-problem-color title-state")
        self.assertNotContains(response, "attempted-problem-color title-state")

    def test_contest_api_masks_hidden_problem_breakdown(self):
        response = self.client.get(f"/api/contest/info/{self.contest.key}")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIsNone(data["rankings"][0]["solutions"][0])

    def test_contest_ranking_csv_masks_hidden_problem_without_faking_attempt(self):
        blank_user = User.objects.create_user("hidden_blank", password="pw")
        blank_profile, _ = Profile.objects.get_or_create(
            user=blank_user, defaults={"language": self.language}
        )
        ContestParticipation.objects.create(
            contest=self.contest,
            user=blank_profile,
            virtual=ContestParticipation.LIVE,
            score=0,
            format_data={},
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("contest_ranking", args=[self.contest.key]), {"format": "csv"}
        )

        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode())))
        header, data_rows = rows[0], rows[1:]
        hidden_label = self.contest.get_label_for_problem(self.contest_problem.order)
        hidden_index = header.index(hidden_label)
        rows_by_user = {row[1]: row for row in data_rows}
        self.assertEqual(rows_by_user[self.user.username][hidden_index], "?")
        self.assertEqual(rows_by_user[blank_user.username][hidden_index], "")

    def test_user_submission_api_masks_public_hidden_result(self):
        self.problem.is_public = True
        self.problem.save(update_fields=["is_public"])

        response = self.client.get(f"/api/user/submissions/{self.user.username}")

        self.assertEqual(response.status_code, 200)
        data = response.json()[str(self.submission.id)]
        self.assertIsNone(data["result"])
        self.assertIsNone(data["status"])
        self.assertIsNone(data["points"])

    def test_user_problem_page_hides_public_hidden_best_submission(self):
        self.problem.is_public = True
        self.problem.save(update_fields=["is_public"])
        self.client.force_login(self.user)

        response = self.client.get(reverse("user_problems", args=[self.user.username]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Hidden Problem")
        self.assertNotContains(response, f"/problem/{self.problem.code}")

    def test_latest_attempted_problems_hide_public_hidden_best_submission(self):
        attempted_problem = Problem.objects.create(
            code="hiddenattempt",
            name="Hidden Attempt",
            group=self.group,
            time_limit=1.0,
            memory_limit=65536,
            points=100,
            is_public=True,
        )
        attempted_contest_problem = ContestProblem.objects.create(
            contest=self.contest,
            problem=attempted_problem,
            points=100,
            order=2,
            is_result_hidden=True,
        )
        attempted_submission = Submission.objects.create(
            user=self.profile,
            problem=attempted_problem,
            language=self.language,
            contest_object=self.contest,
            status="D",
            result="WA",
            points=30,
            case_points=30,
            case_total=100,
            time=0.1,
            memory=1024,
        )
        ContestSubmission.objects.create(
            submission=attempted_submission,
            problem=attempted_contest_problem,
            participation=self.participation,
            points=30,
        )
        BestSubmission.objects.create(
            user=self.profile,
            problem=attempted_problem,
            submission=attempted_submission,
            points=30,
            case_total=100,
        )
        request = RequestFactory().get(reverse("problem_list"))
        request.user = self.user
        request.profile = self.profile
        view = ProblemList()
        view.request = request

        latest_attempts = view.get_latest_attempted_problems()

        self.assertNotIn(
            attempted_problem.code, [problem["code"] for problem in latest_attempts]
        )
