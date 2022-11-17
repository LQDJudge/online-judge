from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _

from judge.models import Profile

__all__ = ["BookMark"]


class BookMark(models.Model):
    page = models.CharField(
        max_length=30,
        verbose_name=_("associated page"),
        db_index=True,
    )

    def get_bookmark(self, user):
        userqueryset = MakeBookMark.objects.filter(bookmark=self, user=user)
        if userqueryset.exists():
            return True
        else:
            return False

    class Meta:
        verbose_name = _("bookmark")
        verbose_name_plural = _("bookmarks")

    def __str__(self):
        return f"bookmark for {self.page}"

class MakeBookMark(models.Model):
    bookmark = models.ForeignKey(BookMark, related_name="bookmark", on_delete=CASCADE)
    user = models.ForeignKey(Profile, related_name="user_bookmark", on_delete=CASCADE, db_index=True)

    class Meta:
        unique_together = ["user", "bookmark"]
        verbose_name = _("make bookmark")
        verbose_name_plural = _("make bookmarks")
