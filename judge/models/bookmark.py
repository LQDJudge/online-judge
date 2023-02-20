from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ObjectDoesNotExist

from judge.models.profile import Profile

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

    def page_object(self):
        from judge.models.contest import Contest
        from judge.models.interface import BlogPost
        from judge.models.problem import Problem, Solution

        try:
            page = self.page
            if page.startswith("p:"):
                return Problem.objects.get(code=page[2:])
            elif page.startswith("c:"):
                return Contest.objects.get(key=page[2:])
            elif page.startswith("b:"):
                return BlogPost.objects.get(id=page[2:])
            elif page.startswith("s:"):
                return Solution.objects.get(problem__code=page[2:])
            return None
        except ObjectDoesNotExist:
            return None

    class Meta:
        verbose_name = _("bookmark")
        verbose_name_plural = _("bookmarks")

    def __str__(self):
        return self.page


class MakeBookMark(models.Model):
    bookmark = models.ForeignKey(BookMark, related_name="bookmark", on_delete=CASCADE)
    user = models.ForeignKey(
        Profile, related_name="user_bookmark", on_delete=CASCADE, db_index=True
    )

    class Meta:
        unique_together = ["user", "bookmark"]
        verbose_name = _("make bookmark")
        verbose_name_plural = _("make bookmarks")
