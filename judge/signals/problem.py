from django.conf import settings
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver
from celery import current_app

from judge.models import Problem, ProblemType
from judge.models.problem import (
    _get_allowed_languages,
    _get_problem_organization_ids,
    _get_problem_types_name,
)
from judge.utils.problems import user_editable_ids, user_tester_ids

SEMANTIC_PROBLEM_FIELDS = {
    "code",
    "name",
    "description",
    "pdf_description",
    "points",
    "partial",
    "time_limit",
    "memory_limit",
    "is_public",
    "is_organization_private",
}


def _schedule_semantic_index(problem_id):
    if getattr(settings, "USE_ML", False):
        transaction.on_commit(
            lambda: current_app.send_task(
                "judge.tasks.semantic_search.index_problem_semantic_embedding",
                args=[problem_id],
            )
        )


def _update_fields_touch_semantic(update_fields, semantic_fields):
    if update_fields is None:
        return True
    return bool(set(update_fields) & semantic_fields)


def _semantic_problem_values(problem):
    return {
        "code": problem.code,
        "name": problem.name,
        "description": problem.description,
        "pdf_description": (
            problem.pdf_description.name if problem.pdf_description else ""
        ),
        "points": problem.points,
        "partial": problem.partial,
        "time_limit": problem.time_limit,
        "memory_limit": problem.memory_limit,
        "is_public": problem.is_public,
        "is_organization_private": problem.is_organization_private,
    }


@receiver(m2m_changed, sender=Problem.organizations.through)
def update_organization_problem(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_organization_private = instance.organizations.exists()
        instance.save(update_fields=["is_organization_private"])
        _get_problem_organization_ids.dirty(instance.id)


@receiver(m2m_changed, sender=Problem.allowed_languages.through)
def update_allowed_languages(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        _get_allowed_languages.dirty(instance.id)


@receiver(m2m_changed, sender=Problem.authors.through)
def update_problem_authors(sender, instance, action, pk_set, **kwargs):
    if action == "pre_clear":
        instance._pre_clear_author_ids = set(instance.get_author_ids())
    elif action in ("post_add", "post_remove"):
        Problem.get_author_ids.dirty(instance)
        for profile_id in pk_set:
            user_editable_ids.dirty(profile_id)
        _schedule_semantic_index(instance.id)
    elif action == "post_clear":
        Problem.get_author_ids.dirty(instance)
        for profile_id in getattr(instance, "_pre_clear_author_ids", ()):
            user_editable_ids.dirty(profile_id)
        _schedule_semantic_index(instance.id)


@receiver(m2m_changed, sender=Problem.curators.through)
def update_problem_curators(sender, instance, action, pk_set, **kwargs):
    if action == "pre_clear":
        instance._pre_clear_curator_ids = set(instance.get_curator_ids())
    elif action in ("post_add", "post_remove"):
        for profile_id in pk_set:
            user_editable_ids.dirty(profile_id)
    elif action == "post_clear":
        for profile_id in getattr(instance, "_pre_clear_curator_ids", ()):
            user_editable_ids.dirty(profile_id)


@receiver(m2m_changed, sender=Problem.testers.through)
def update_problem_testers(sender, instance, action, pk_set, **kwargs):
    if action == "pre_clear":
        instance._pre_clear_tester_ids = set(instance.get_tester_ids())
    elif action in ("post_add", "post_remove"):
        for profile_id in pk_set:
            user_tester_ids.dirty(profile_id)
    elif action == "post_clear":
        for profile_id in getattr(instance, "_pre_clear_tester_ids", ()):
            user_tester_ids.dirty(profile_id)


@receiver(m2m_changed, sender=Problem.types.through)
def problem_types_changed(sender, instance, action, pk_set, **kwargs):
    """
    Signal handler to clear cache when problem types are added/removed from problems.
    This automatically handles cache invalidation for _get_problem_types_name.
    """
    if action in ("post_add", "post_remove", "post_clear"):
        _get_problem_types_name.dirty(instance.id)
        _schedule_semantic_index(instance.id)


@receiver(pre_save, sender=Problem)
def problem_semantic_index_capture_previous(sender, instance, **kwargs):
    if not getattr(settings, "USE_ML", False) or not instance.pk:
        return
    try:
        previous = Problem.objects.only(*SEMANTIC_PROBLEM_FIELDS).get(pk=instance.pk)
    except Problem.DoesNotExist:
        return
    instance._semantic_previous_values = _semantic_problem_values(previous)


@receiver(post_save, sender=Problem)
def problem_semantic_index_update(sender, instance, created, update_fields, **kwargs):
    if not _update_fields_touch_semantic(update_fields, SEMANTIC_PROBLEM_FIELDS):
        return

    if not created:
        previous_values = getattr(instance, "_semantic_previous_values", None)
        if previous_values == _semantic_problem_values(instance):
            return

    _schedule_semantic_index(instance.id)


@receiver(post_delete, sender=Problem)
def problem_semantic_index_delete(sender, instance, **kwargs):
    _schedule_semantic_index(instance.id)


@receiver(post_save, sender=ProblemType)
def problem_type_semantic_index_update(sender, instance, **kwargs):
    if not getattr(settings, "USE_ML", False):
        return
    problem_ids = Problem.types.through.objects.filter(
        problemtype_id=instance.id
    ).values_list("problem_id", flat=True)
    for problem_id in problem_ids:
        _schedule_semantic_index(problem_id)
