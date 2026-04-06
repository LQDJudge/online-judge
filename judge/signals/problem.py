from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from judge.models import Problem
from judge.models.comment import get_content_author_ids
from judge.models.problem import (
    _get_allowed_languages,
    _get_problem_organization_ids,
    _get_problem_types_name,
)
from judge.utils.problems import user_editable_ids, user_tester_ids


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
        ct = ContentType.objects.get_for_model(Problem)
        get_content_author_ids.dirty(ct.id, instance.id)
        for profile_id in pk_set:
            user_editable_ids.dirty(profile_id)
    elif action == "post_clear":
        Problem.get_author_ids.dirty(instance)
        ct = ContentType.objects.get_for_model(Problem)
        get_content_author_ids.dirty(ct.id, instance.id)
        for profile_id in getattr(instance, "_pre_clear_author_ids", ()):
            user_editable_ids.dirty(profile_id)


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
