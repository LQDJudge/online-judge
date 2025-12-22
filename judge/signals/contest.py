from django.db.models.signals import post_delete, m2m_changed
from django.dispatch import receiver

from judge.models import Contest, ContestProblem, Submission
from judge.models.contest import _get_contest_organization_ids


@receiver(m2m_changed, sender=Contest.organizations.through)
def update_organization_private(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_organization_private = instance.organizations.exists()
        instance.save(update_fields=["is_organization_private"])


@receiver(m2m_changed, sender=Contest.private_contestants.through)
def update_private(sender, instance, **kwargs):
    if kwargs["action"] in ["post_add", "post_remove", "post_clear"]:
        instance.is_private = instance.private_contestants.exists()
        instance.save(update_fields=["is_private"])


@receiver(m2m_changed, sender=Contest.organizations.through)
def on_contest_organization_change(sender, instance, action, **kwargs):
    if action in ["post_add", "post_remove", "post_clear"]:
        if isinstance(instance, Contest):
            _get_contest_organization_ids.dirty(instance.id)


@receiver(post_delete, sender=ContestProblem)
def contest_problem_delete(sender, instance, **kwargs):
    Submission.objects.filter(
        contest_object=instance.contest, contest__isnull=True
    ).update(contest_object=None)
