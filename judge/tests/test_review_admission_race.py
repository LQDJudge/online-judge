"""Real database-lock admission tests; no judge, broker or model calls."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TransactionTestCase, skipUnlessDBFeature, override_settings
from django.utils import timezone

from judge.models import (
    Contest,
    ContestProblem,
    Language,
    Problem,
    ProblemGroup,
    Profile,
)
from judge.review import workflow


@override_settings(AUTO_REVIEW_COORDINATOR_ENABLED=True)
class ReviewAdmissionRaceTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        language, _ = Language.objects.get_or_create(
            key="PY3",
            defaults={
                "name": "Python 3",
                "short_name": "PY3",
                "common_name": "Python",
                "ace": "python",
                "pygments": "python3",
            },
        )
        group = ProblemGroup.objects.create(
            name="review-race", full_name="Review admission race"
        )
        self.problem = Problem.objects.create(
            code="reviewrace",
            name="Admission race",
            description="Original package",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=1,
        )
        self.actors = []
        for index in range(2):
            user = User.objects.create_user("review_race_%s" % index)
            user.user_permissions.add(Permission.objects.get(codename="add_problem"))
            actor, _ = Profile.objects.get_or_create(
                user=user, defaults={"language": language}
            )
            self.problem.authors.add(actor)
            self.actors.append(actor.pk)

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_authors_cannot_admit_two_simultaneous_reviews(self):
        barrier = Barrier(2)
        snapshot = {
            "kind": "problem",
            "statement": "Original package",
            "content_hash": "a" * 64,
            "input_hash": "b" * 64,
        }

        def start(actor_id):
            close_old_connections()
            try:
                actor = Profile.objects.select_related("user").get(pk=actor_id)
                problem = Problem.objects.get(pk=self.problem.pk)
                barrier.wait(timeout=10)
                try:
                    run = workflow.start_problem_review(
                        problem, actor, confirm_unchanged=True
                    )
                    return "admitted", run.pk
                except ValidationError:
                    return "blocked", None
            finally:
                connections.close_all()

        with patch(
            "judge.review.workflow.build_problem_snapshot", return_value=snapshot
        ), patch("judge.review.workflow.preparation_errors", return_value=[]), patch(
            "judge.review.workflow.enqueue"
        ) as enqueue:
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(start, self.actors))
        self.assertCountEqual(
            [result[0] for result in outcomes], ["admitted", "blocked"]
        )
        self.assertEqual(self.problem.review_runs.count(), 1)
        self.assertEqual(self.problem.review_runs.get().status, "R")
        enqueue.assert_called_once()

    @skipUnlessDBFeature("has_select_for_update")
    def test_two_contest_authors_share_one_active_slot(self):
        contest = Contest.objects.create(
            key="racecontest",
            name="Contest admission race",
            start_time=timezone.now(),
            end_time=timezone.now() + timezone.timedelta(hours=2),
        )
        contest.authors.add(*self.actors)
        ContestProblem.objects.create(
            contest=contest, problem=self.problem, points=100, order=0
        )
        barrier = Barrier(2)

        def start(actor_id):
            close_old_connections()
            try:
                actor = Profile.objects.select_related("user").get(pk=actor_id)
                current = Contest.objects.get(pk=contest.pk)
                barrier.wait(timeout=10)
                try:
                    run = workflow.start_contest_review(
                        current, actor, confirm_unchanged=True
                    )
                    return "admitted", run.pk
                except ValidationError:
                    return "blocked", None
            finally:
                connections.close_all()

        with patch("judge.review.workflow.enqueue") as enqueue:
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(start, self.actors))
        self.assertCountEqual(
            [result[0] for result in outcomes], ["admitted", "blocked"]
        )
        self.assertEqual(contest.review_runs.count(), 1)
        self.assertEqual(contest.review_runs.get().child_dependencies.count(), 1)
        self.assertEqual(self.problem.review_runs.count(), 0)
        enqueue.assert_called_once()
