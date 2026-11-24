from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TransactionTestCase, override_settings

from judge.models import Course, CourseLessonPrerequisite


@override_settings(MIGRATION_MODULES={})
class CourseGradeSchemaMigrationTests(TransactionTestCase):
    def test_legacy_courses_default_to_shared_and_prerequisites_are_preserved(self):
        loader = MigrationLoader(connection)
        before = loader.project_state([("judge", "0282_moderation_queue_state")])
        migration = loader.disk_migrations[
            ("judge", "0283_course_grade_privacy_and_prerequisite_identity")
        ]
        old_course = before.apps.get_model("judge", "Course")
        old_lesson = before.apps.get_model("judge", "CourseLesson")
        old_edge = before.apps.get_model("judge", "CourseLessonPrerequisite")
        applied = True
        try:
            with connection.schema_editor() as editor:
                migration.unapply(before.clone(), editor)
            applied = False
            course = old_course.objects.create(
                name="Existing course",
                slug="migration-existing",
                about="Original description",
            )
            for order in (1, 2):
                old_lesson.objects.create(
                    course=course, title=str(order), content="", points=100, order=order
                )
            edge = old_edge.objects.create(
                course=course, source_order=1, target_order=2, required_percentage=80
            )
            with connection.schema_editor() as editor:
                migration.apply(before.clone(), editor)
            applied = True
            self.assertTrue(Course.objects.get(pk=course.pk).show_classmates_grades)
            current_edge = CourseLessonPrerequisite.objects.get(pk=edge.pk)
            self.assertEqual(
                (current_edge.source_order, current_edge.target_order), (1, 2)
            )
            self.assertIsNone(current_edge.source_lesson_id)
            self.assertIsNone(current_edge.target_lesson_id)
            default = Course.objects.create(
                name="New course", slug="migration-new", about=""
            )
            self.assertTrue(default.show_classmates_grades)
            default.delete()
            with connection.schema_editor() as editor:
                migration.unapply(before.clone(), editor)
            applied = False
            self.assertEqual(
                old_course.objects.get(pk=course.pk).about, "Original description"
            )
            self.assertEqual(old_edge.objects.get(pk=edge.pk).required_percentage, 80)
        finally:
            if not applied:
                with connection.schema_editor() as editor:
                    migration.apply(before.clone(), editor)
