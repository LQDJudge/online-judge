from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from judge.models import Problem
from judge.models.problem import (
    _get_allowed_languages,
    _get_problem_organization_ids,
    _get_problem_types_name,
)


@receiver(m2m_changed, sender=Problem.organizations.through)
def update_organization_problem(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_organization_private = instance.organizations.exists()
        instance.save(update_fields=["is_organization_private"])
        _get_problem_organization_ids.dirty(instance.id)


@receiver(m2m_changed, sender=Problem.allowed_languages.through)
def update_allowed_languages(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        _get_allowed_languages.dirty((instance.id,))


@receiver(m2m_changed, sender=Problem.authors.through)
def update_problem_authors(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        Problem.get_author_ids.dirty(instance)


@receiver(m2m_changed, sender=Problem.types.through)
def problem_types_changed(sender, instance, action, pk_set, **kwargs):
    """
    Signal handler to clear cache when problem types are added/removed from problems.
    This automatically handles cache invalidation for _get_problem_types_name.
    """
    if action in ("post_add", "post_remove", "post_clear"):
        _get_problem_types_name.dirty(instance.id)
