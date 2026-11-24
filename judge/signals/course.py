from functools import partial

from django.db import transaction
from django.db.models.signals import post_save, post_delete, pre_save
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
        transaction.on_commit(
            partial(CourseLesson.get_problems_and_scores.dirty, instance.lesson_id)
        )


def dirty_course_roles(pairs):
    for course_id, role in pairs:
        get_course_role_profile_ids.dirty(course_id, role)


@receiver([post_save, post_delete], sender=CourseRole)
def invalidate_course_role_cache(sender, instance, **kwargs):
    pairs = {(instance.course_id, instance.role)}
    previous = getattr(instance, "_previous_course_role", None)
    if previous:
        pairs.add(previous)
    dirty_course_roles(pairs)
    transaction.on_commit(partial(dirty_course_roles, pairs))


@receiver(pre_save, sender=CourseRole)
def remember_previous_course_role(sender, instance, **kwargs):
    if instance.pk:
        instance._previous_course_role = (
            CourseRole.objects.filter(pk=instance.pk)
            .values_list("course_id", "role")
            .first()
        )
