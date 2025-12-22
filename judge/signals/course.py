from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from judge.models import CourseContest
from judge.models.course import (
    CourseLesson,
    CourseLessonProblem,
    CourseRole,
    get_course_role_profile_ids,
)


@receiver(post_delete, sender=CourseContest)
def course_contest_delete(sender, instance, **kwargs):
    instance.contest.delete()


@receiver([post_save, post_delete], sender=CourseLessonProblem)
def invalidate_lesson_problems_cache(sender, instance, **kwargs):
    """Invalidate the cached problems and scores list when a CourseLessonProblem is saved or deleted."""
    if instance.lesson_id:
        CourseLesson.get_problems_and_scores.dirty(instance.lesson_id)


@receiver([post_save, post_delete], sender=CourseRole)
def invalidate_course_role_cache(sender, instance, **kwargs):
    """Invalidate the cached course role profile IDs when a CourseRole is saved or deleted."""
    if instance.course_id and instance.role:
        get_course_role_profile_ids.dirty(instance.course_id, instance.role)
