from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from judge.models.profile import Profile
from judge.caching import cache_wrapper

__all__ = ["Block"]


class Block(models.Model):
    blocker_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="blocker_blocks"
    )
    blocker_id = models.PositiveIntegerField()

    blocked_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, related_name="blocked_blocks"
    )
    blocked_id = models.PositiveIntegerField()

    class Meta:
        verbose_name = _("block")
        verbose_name_plural = _("blocks")
        indexes = [
            models.Index(fields=["blocker_type", "blocker_id"], name="blocker_idx"),
            models.Index(fields=["blocked_type", "blocked_id"], name="blocked_idx"),
        ]
        unique_together = (
            ("blocker_type", "blocker_id", "blocked_type", "blocked_id"),
        )

    def __str__(self):
        return f"{self.get_blocker()} blocked {self.get_blocked()}"

    def get_blocker(self):
        """Retrieve the blocker object (Profile or Organization)."""
        return self.blocker_type.get_object_for_this_type(pk=self.blocker_id)

    def get_blocked(self):
        """Retrieve the blocked object (Profile or Organization)."""
        return self.blocked_type.get_object_for_this_type(pk=self.blocked_id)

    @classmethod
    def is_blocked(self, blocker, blocked):
        """Check if a blocker has blocked a blocked entity."""
        blocked_type = ContentType.objects.get_for_model(type(blocked))
        blocked_pair = (blocked_type.id, blocked.id)
        return blocked_pair in get_all_blocked_pairs(blocker)

    @classmethod
    def add_block(self, blocker, blocked):
        if blocker == blocked:
            raise ValueError("A user or organization cannot block itself.")

        if self.is_blocked(blocker, blocked):
            raise ValueError("You have already blocked this user or organization.")

        blocker_type = ContentType.objects.get_for_model(type(blocker))
        blocked_type = ContentType.objects.get_for_model(type(blocked))

        Block.objects.create(
            blocker_type=blocker_type,
            blocker_id=blocker.id,
            blocked_type=blocked_type,
            blocked_id=blocked.id,
        )
        get_all_blocked_pairs.dirty(blocker)

    @classmethod
    def remove_block(self, blocker, blocked):
        if blocker == blocked:
            raise ValueError("A user or organization cannot unblock itself.")

        if not self.is_blocked(blocker, blocked):
            raise ValueError(
                "This user or organization is not blocked, so it cannot be unblocked."
            )

        blocker_type = ContentType.objects.get_for_model(type(blocker))
        blocked_type = ContentType.objects.get_for_model(type(blocked))

        Block.objects.filter(
            blocker_type=blocker_type,
            blocker_id=blocker.id,
            blocked_type=blocked_type,
            blocked_id=blocked.id,
        ).delete()
        get_all_blocked_pairs.dirty(blocker)


@cache_wrapper(prefix="gablp")
def get_all_blocked_pairs(blocker):
    """
    Returns a set of all (blocked_type, blocked_id) pairs that a blocker has blocked.
    """
    blocker_type = ContentType.objects.get_for_model(type(blocker))
    return set(
        Block.objects.filter(
            blocker_type=blocker_type,
            blocker_id=blocker.id,
        ).values_list("blocked_type_id", "blocked_id")
    )
