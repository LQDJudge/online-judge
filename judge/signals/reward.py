from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from judge.models import AvatarFrame, ProfileFrame
from judge.utils.rewards import invalidate_equipped_frame


@receiver(pre_save, sender=ProfileFrame)
def invalidate_previous_frame_owner(sender, instance, **kwargs):
    if instance.pk:
        previous = (
            ProfileFrame.objects.filter(pk=instance.pk)
            .values_list("profile_id", flat=True)
            .first()
        )
        if previous and previous != instance.profile_id:
            invalidate_equipped_frame(previous)


@receiver([post_save, post_delete], sender=ProfileFrame)
def invalidate_awarded_frame(sender, instance, **kwargs):
    invalidate_equipped_frame(instance.profile_id)


@receiver([post_save, post_delete], sender=AvatarFrame)
def invalidate_frame_catalog(sender, instance, **kwargs):
    for profile_id in ProfileFrame.objects.filter(
        frame=instance, is_equipped=True
    ).values_list("profile_id", flat=True):
        invalidate_equipped_frame(profile_id)
