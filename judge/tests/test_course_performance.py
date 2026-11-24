"""Guard batch loading and freshness of lesson scoring metadata."""

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import caches
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from judge.models import (
    Course,
    CourseLesson,
    CourseLessonProblem,
    CourseRole,
    Language,
    Profile,
    Problem,
    ProblemGroup,
)
from judge.models.course import get_course_role_profile_ids
from judge.utils.course_grades import _prepare_lesson_progress_data


@override_settings(
    CACHES={
        alias: {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "course-performance-regression-" + alias,
        }
        for alias in settings.CACHES
    }
)
class LessonScoreBatchTests(TransactionTestCase):
    fixtures = ["language_small"]

    def setUp(self):
        for cache in caches.all():
            cache.clear()
        self.course = Course.objects.create(name="Batch scores", slug="batch-scores")
        group = ProblemGroup.objects.create(name="Batch", full_name="Batch")
        self.problem = Problem.objects.create(
            code="batchscore",
            name="Batch score",
            group=group,
            time_limit=1,
            memory_limit=65536,
            points=100,
        )
        self.lessons = CourseLesson.objects.bulk_create(
            [
                CourseLesson(course=self.course, title=str(i), order=i + 1, points=100)
                for i in range(20)
            ]
        )
        CourseLessonProblem.objects.bulk_create(
            [
                CourseLessonProblem(
                    lesson=lesson, problem=self.problem, order=1, score=10 + i
                )
                for i, lesson in enumerate(self.lessons[:-1])
            ]
        )

    def batch(self, lessons):
        return CourseLesson.get_problems_and_scores.batch(
            [(lesson,) for lesson in lessons]
        )

    def test_cold_batch_uses_one_query_and_caches_empty_lessons(self):
        # Input order may differ from the database's lesson order.
        lessons = list(reversed(self.lessons))
        with self.assertNumQueries(1):
            rows = self.batch(lessons)
        self.assertEqual(rows[0], [])
        self.assertEqual(rows[-1], [{"problem_id": self.problem.pk, "score": 10}])
        with self.assertNumQueries(0):
            self.assertEqual(self.batch(lessons), rows)
            self.assertEqual(self.lessons[-1].get_problems_and_scores(), [])

    def test_partial_cache_miss_preserves_fresh_scores(self):
        self.batch(self.lessons)
        row = CourseLessonProblem.objects.get(lesson=self.lessons[0])
        row.score = 75
        row.save()
        with self.assertNumQueries(1):
            rows = self.batch(self.lessons)
        self.assertEqual(rows[0][0]["score"], 75)
        self.assertEqual(rows[1][0]["score"], 11)
        row.delete()
        with self.assertNumQueries(1):
            self.assertEqual(self.batch(self.lessons)[0], [])

    def test_preparing_more_lessons_does_not_add_per_lesson_queries(self):
        counts = []
        for lessons in [self.lessons[:2], self.lessons]:
            for cache in caches.all():
                cache.clear()
            with CaptureQueriesContext(connection) as queries:
                result = _prepare_lesson_progress_data([], lessons)
            counts.append(len(queries))
            self.assertEqual(len(result["lesson_problems"]), len(lessons))
        self.assertEqual(counts[0], counts[1])

    def test_empty_batch_does_not_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(self.batch([]), [])

    def test_rolled_back_score_does_not_leak_into_shared_cache(self):
        lesson = self.lessons[0]
        self.assertEqual(self.batch([lesson])[0][0]["score"], 10)
        with transaction.atomic():
            row = CourseLessonProblem.objects.get(lesson=lesson)
            row.score = 90
            row.save()
            self.assertEqual(self.batch([lesson])[0][0]["score"], 90)
            transaction.set_rollback(True)
        self.assertEqual(self.batch([lesson])[0][0]["score"], 10)

    def test_commit_clears_metadata_cached_by_another_reader(self):
        lesson = self.lessons[0]
        old_rows = self.batch([lesson])
        with transaction.atomic():
            row = CourseLessonProblem.objects.get(lesson=lesson)
            row.score = 90
            row.save()
            # Simulate another request caching the committed old value while
            # this transaction is still open.
            for cache in caches.all():
                cache.set("CLgps:%s" % lesson.pk, old_rows[0])
        self.assertEqual(self.batch([lesson])[0][0]["score"], 90)

    def test_rolled_back_role_change_does_not_poison_roster_cache(self):
        user = User.objects.create(username="rollback_roster")
        profile, _ = Profile.objects.get_or_create(
            user=user, defaults={"language": Language.objects.first()}
        )
        role = CourseRole.objects.create(course=self.course, user=profile, role="ST")
        self.assertEqual(
            get_course_role_profile_ids(self.course.pk, "ST"), [profile.pk]
        )
        with transaction.atomic():
            role.role = "AS"
            role.save()
            self.assertEqual(
                get_course_role_profile_ids(self.course.pk, "AS"), [profile.pk]
            )
            self.assertEqual(get_course_role_profile_ids(self.course.pk, "ST"), [])
            transaction.set_rollback(True)
        self.assertEqual(
            get_course_role_profile_ids(self.course.pk, "ST"), [profile.pk]
        )
        self.assertEqual(get_course_role_profile_ids(self.course.pk, "AS"), [])
