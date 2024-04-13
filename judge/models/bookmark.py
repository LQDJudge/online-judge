from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

from judge.models.profile import Profile
from judge.caching import cache_wrapper

__all__ = ["BookMark"]


class BookMark(models.Model):
    page = models.CharField(
        max_length=30,
        verbose_name=_("associated page"),
        db_index=True,
    )  # deprecated
    score = models.IntegerField(verbose_name=_("votes"), default=0)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    linked_object = GenericForeignKey("content_type", "object_id")

    @cache_wrapper(prefix="BMgb")
    def get_bookmark(self, user):
        return MakeBookMark.objects.filter(bookmark=self, user=user).exists()

    class Meta:
        verbose_name = _("bookmark")
        verbose_name_plural = _("bookmarks")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        unique_together = ("content_type", "object_id")

    def __str__(self):
        return f"bookmark for {self.linked_object}"


class MakeBookMark(models.Model):
    bookmark = models.ForeignKey(BookMark, related_name="bookmark", on_delete=CASCADE)
    user = models.ForeignKey(
        Profile, related_name="user_bookmark", on_delete=CASCADE, db_index=True
    )

    class Meta:
        indexes = [
            models.Index(fields=["user", "bookmark"]),
        ]
        unique_together = ["user", "bookmark"]
        verbose_name = _("make bookmark")
        verbose_name_plural = _("make bookmarks")


@cache_wrapper(prefix="gocb")
def _get_or_create_bookmark(content_type, object_id):
    bookmark, created = BookMark.objects.get_or_create(
        content_type=content_type,
        object_id=object_id,
    )
    return bookmark


class Bookmarkable:
    def get_or_create_bookmark(self):
        content_type = ContentType.objects.get_for_model(self)
        object_id = self.pk
        return _get_or_create_bookmark(content_type, object_id)


def dirty_bookmark(bookmark, profile):
    bookmark.get_bookmark.dirty(bookmark, profile)
    _get_or_create_bookmark.dirty(bookmark.content_type, bookmark.object_id)
