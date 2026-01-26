from django.db import models
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

from judge.models.profile import Profile
from judge.caching import cache_wrapper

__all__ = ["BookMark"]


class BookMark(models.Model):
    score = models.IntegerField(verbose_name=_("votes"), default=0)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    linked_object = GenericForeignKey("content_type", "object_id")
    users = models.ManyToManyField(
        Profile, related_name="bookmarked_objects", blank=True
    )

    def is_bookmarked_by(self, profile):
        return self.id in get_all_bookmarked_object_ids(profile)

    def add_bookmark(self, profile):
        """Adds a bookmark for a user and increments the score."""
        if not self.is_bookmarked_by(profile):
            self.users.add(profile)
            self.score = models.F("score") + 1
            self.save(update_fields=["score"])
            get_all_bookmarked_object_ids.dirty(profile)

    def remove_bookmark(self, profile):
        """Removes a bookmark for a user and decrements the score."""
        if self.is_bookmarked_by(profile):
            self.users.remove(profile)
            self.score = models.F("score") - 1
            self.save(update_fields=["score"])
            get_all_bookmarked_object_ids.dirty(profile)

    class Meta:
        verbose_name = _("bookmark")
        verbose_name_plural = _("bookmarks")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        unique_together = ("content_type", "object_id")

    def __str__(self):
        return f"bookmark for {self.linked_object}"


@cache_wrapper(prefix="gocb", expected_type=BookMark)
def _get_or_create_bookmark(content_type, object_id):
    bookmark, created = BookMark.objects.get_or_create(
        content_type=content_type,
        object_id=object_id,
    )
    return bookmark


@cache_wrapper(prefix="gaboi")
def get_all_bookmarked_object_ids(profile):
    return set(profile.bookmarked_objects.values_list("id", flat=True))


class Bookmarkable:
    def get_or_create_bookmark(self):
        content_type = ContentType.objects.get_for_model(self)
        object_id = self.pk
        return _get_or_create_bookmark(content_type, object_id)
