"""
Management command to recalculate all course lesson grades for all enrolled users.
Run this after migrations that populate BestSubmission/BestQuizAttempt tables.
"""

from django.core.management.base import BaseCommand
from judge.models import Course, CourseRole
from judge.utils.course_prerequisites import (
    update_lesson_grade,
    update_lesson_unlock_states,
)


class Command(BaseCommand):
    help = (
        "Recalculate all course lesson grades and unlock states for all enrolled users"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--course",
            type=str,
            help="Specific course slug to recalculate (default: all courses)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only show what would be updated, don't actually update",
        )

    def handle(self, *args, **options):
        course_slug = options.get("course")
        dry_run = options.get("dry_run", False)

        if course_slug:
            courses = Course.objects.filter(slug=course_slug)
            if not courses.exists():
                self.stderr.write(f"Course '{course_slug}' not found")
                return
        else:
            courses = Course.objects.all()

        total_updated = 0
        total_unlocked = 0

        for course in courses:
            self.stdout.write(f"\nProcessing course: {course.name} ({course.slug})")

            # Get all enrolled users
            enrolled_roles = CourseRole.objects.filter(course=course).select_related(
                "user"
            )
            lessons = list(course.lessons.all().order_by("order"))

            self.stdout.write(f"  {enrolled_roles.count()} enrolled users")
            self.stdout.write(f"  {len(lessons)} lessons")

            for role in enrolled_roles:
                user = role.user

                # Update grades for each lesson
                for lesson in lessons:
                    if not dry_run:
                        update_lesson_grade(user, lesson)

                # Update unlock states
                if not dry_run:
                    newly_unlocked = update_lesson_unlock_states(user, course)
                    if newly_unlocked:
                        total_unlocked += len(newly_unlocked)

                total_updated += 1

            self.stdout.write(f"  Processed {enrolled_roles.count()} users")

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"\nDry run complete. Would update {total_updated} user-course combinations."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"\nRecalculation complete. Updated {total_updated} user-course combinations. "
                    f"Unlocked {total_unlocked} lessons."
                )
            )
