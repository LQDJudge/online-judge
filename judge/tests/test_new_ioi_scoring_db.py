"""Real SQL regressions for the parent's isolated Django test database.

Run with reviewed local settings:
python3 manage.py test judge.tests.test_new_ioi_scoring_db
These fixtures do not submit code, start judges, or write problem-data files.
"""

from datetime import datetime, timedelta, timezone

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from judge.contest_format.new_ioi import NewIOIContestFormat
from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Language,
    Problem,
    ProblemGroup,
    ProblemTestCase,
    Profile,
    Submission,
    SubmissionTestCase,
)
from judge.timezone import from_database_time


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    EVENT_DAEMON_USE=False,
    USE_ML=False,
    DMOJ_PROBLEM_DATA_PUSH_UPDATE=False,
)
class NewIOIScoringDatabaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.language, _created = Language.objects.get_or_create(
            key="PY3",
            defaults=dict(
                name="Python 3",
                short_name="PY3",
                common_name="Python",
                ace="python",
                pygments="python3",
                template="",
            ),
        )
        cls.group = ProblemGroup.objects.create(
            name="ioi16sql", full_name="IOI16 SQL tests"
        )
        cls.user = User.objects.create_user("ioi16sql")
        cls.profile, _created = Profile.objects.get_or_create(
            user=cls.user, defaults={"language": cls.language}
        )
        cls.start = datetime(2025, 3, 23, 0, tzinfo=timezone.utc)
        cls.contest = Contest.objects.create(
            key="ioi16sql",
            name="IOI16 SQL tests",
            start_time=cls.start,
            end_time=cls.start + timedelta(hours=5),
            time_limit=timedelta(hours=5),
            format_name="ioi16",
            is_visible=False,
            is_private=True,
            points_precision=2,
        )
        cls.participation = ContestParticipation.objects.create(
            contest=cls.contest,
            user=cls.profile,
            real_start=cls.start,
            virtual=ContestParticipation.LIVE,
        )

    def make_problem(self, code, modes, order=0, hidden_subtasks=""):
        problem = Problem(
            code=code,
            name=code,
            group=self.group,
            time_limit=1,
            memory_limit=65536,
            points=100,
            partial=True,
            is_public=False,
        )
        problem._bypass_points_cap = True
        problem.save()
        cp = ContestProblem.objects.create(
            contest=self.contest,
            problem=problem,
            points=100,
            order=order,
            partial=True,
            max_submissions=0,
            hidden_subtasks=hidden_subtasks,
        )
        # Noncontiguous order values catch accidental use of order as batch ID.
        ProblemTestCase.objects.bulk_create(
            [
                ProblemTestCase(
                    dataset=problem,
                    order=(i + 1) * 10,
                    type="S",
                    batch_scoring=mode,
                    points=100,
                    input_file="",
                    output_file="",
                    is_pretest=False,
                )
                for i, mode in enumerate(modes)
            ]
        )
        return cp

    def submit(self, cp, minute, cases, status="D", participation=None):
        participation = participation or self.participation
        submission = Submission.objects.create(
            user=self.profile,
            problem_id=cp.problem_id,
            language=self.language,
            contest_object=self.contest,
            status=status,
            result="WA",
            points=0,
        )
        Submission.objects.filter(pk=submission.pk).update(
            date=self.start + timedelta(minutes=minute)
        )
        ContestSubmission.objects.create(
            submission=submission, problem=cp, participation=participation, points=0
        )
        SubmissionTestCase.objects.bulk_create(
            [
                SubmissionTestCase(
                    submission=submission,
                    case=i + 1,
                    batch=batch,
                    points=points,
                    total=total,
                    status="AC" if points else "WA",
                    time=0,
                    memory=0,
                )
                for i, (batch, points, total) in enumerate(cases)
            ]
        )
        return submission

    def results(self, include_frozen=False):
        format = NewIOIContestFormat(self.contest, {})
        with self.assertNumQueries(2):
            rows = format.get_results_by_subtask(self.participation, include_frozen)
        return {(row[0], row[5]): row for row in rows}

    def test_sql_mixed_min_sum_and_null_groups_choose_independent_bests(self):
        cp = self.make_problem("ioi16mixed", ["min", "sum"])
        first = self.submit(
            cp,
            10,
            [
                (1, 50, 50),
                (1, 0, 50),
                (2, 50, 50),
                (2, 0, 50),
                (None, 3, 10),
                (None, 7, 10),
            ],
        )
        second = self.submit(
            cp,
            20,
            [
                (1, 20, 50),
                (1, 20, 50),
                (2, 20, 50),
                (2, 20, 50),
                (None, 4, 10),
                (None, 4, 10),
            ],
        )
        rows = self.results()
        self.assertEqual(rows[(cp.id, 1)][3:5], (40, 100))
        self.assertEqual(rows[(cp.id, 1)][6], second.id)
        self.assertEqual(rows[(cp.id, 2)][3:5], (50, 100))
        self.assertEqual(rows[(cp.id, 2)][6], first.id)
        self.assertEqual(rows[(cp.id, None)][3:5], (10, 20))
        self.assertEqual(rows[(cp.id, None)][6], first.id)

    def test_sql_unequal_weights_and_zero_total_follow_bridge(self):
        cp = self.make_problem("ioi16zero", ["min", "min", "min"])
        self.submit(
            cp, 1, [(1, 10, 20), (1, 20, 80), (2, 0, 0), (2, 100, 100), (3, 5, 0)]
        )
        rows = self.results()
        self.assertEqual(rows[(cp.id, 1)][3:5], (25, 100))
        self.assertEqual(rows[(cp.id, 2)][3:5], (0, 100))
        self.assertEqual(rows[(cp.id, 3)][3:5], (5, 0))

    def test_sql_ties_keep_date_id_and_maximum_from_same_submission(self):
        cp = self.make_problem("ioi16ties", ["min"])
        self.submit(cp, 30, [(1, 50, 100)])
        earliest = self.submit(cp, 20, [(1, 50, 200)])
        self.submit(cp, 20, [(1, 50, 100)])
        self.submit(cp, 10, [(1, 20, 100)])
        row = self.results()[(cp.id, 1)]
        self.assertEqual(row[3:5], (50, 200))
        self.assertEqual(row[6], earliest.id)
        self.assertEqual(from_database_time(row[2]), self.start + timedelta(minutes=20))

    def test_sql_batch_numbers_are_per_problem_and_query_count_is_constant(self):
        first = self.make_problem("ioi16first", ["sum", "min"])
        second = self.make_problem("ioi16second", ["min", "sum"], order=1)
        for cp in (first, second):
            self.submit(cp, 1, [(1, 50, 50), (1, 0, 50), (2, 50, 50), (2, 0, 50)])
        rows = self.results()
        self.assertEqual(
            [rows[(cp.id, batch)][3] for cp in (first, second) for batch in (1, 2)],
            [50, 0, 0, 50],
        )

    def test_sql_filters_status_participation_and_strict_freeze_boundaries(self):
        self.contest.freeze_after = timedelta(minutes=60)
        cp = self.make_problem("ioi16freeze", ["min"])
        before = self.submit(cp, 59, [(1, 20, 100)])
        self.submit(cp, 60, [(1, 40, 100)])
        final = self.submit(cp, 299, [(1, 60, 100)])
        self.submit(cp, 300, [(1, 100, 100)])
        self.submit(cp, 1, [(1, 100, 100)], status="G")
        other = ContestParticipation.objects.create(
            contest=self.contest, user=self.profile, real_start=self.start, virtual=1
        )
        self.submit(cp, 1, [(1, 100, 100)], participation=other)
        self.assertEqual(self.results()[(cp.id, 1)][6], before.id)
        self.assertEqual(self.results(include_frozen=True)[(cp.id, 1)][6], final.id)

    def test_hidden_and_frozen_update_uses_corrected_sql_for_public_and_final(self):
        self.contest.freeze_after = timedelta(minutes=60)
        cp = self.make_problem("ioi16hidden", ["min", "sum"], hidden_subtasks="2")
        self.submit(cp, 10, [(1, 20, 25), (1, 10, 25), (2, 30, 50)])
        self.submit(cp, 70, [(1, 50, 50), (2, 50, 50)])
        format = NewIOIContestFormat(self.contest, {"cumtime": True})
        format.update_participation(self.participation)
        self.participation.refresh_from_db()
        self.assertEqual(self.participation.score, 20)
        self.assertEqual(self.participation.score_final, 100)
        self.assertEqual(self.participation.cumtime, 600)
        self.assertEqual(self.participation.cumtime_final, 4200)
        self.assertEqual(
            self.participation.format_data[str(cp.id)]["total_points"], 100
        )

    def test_sql_singleton_multi_fractions_and_best_across_submissions(self):
        cp = self.make_problem("ioi16multi", ["min", "min", "min"])
        first = self.submit(cp, 1, [(1, 16, 16), (2, 3, 60), (3, 0, 24)])
        second = self.submit(cp, 2, [(1, 9, 16), (2, 57, 60), (3, 24, 24)])
        rows = self.results()
        self.assertEqual(
            [(rows[(cp.id, b)][3], rows[(cp.id, b)][6]) for b in (1, 2, 3)],
            [(16, first.id), (57, second.id), (24, second.id)],
        )
