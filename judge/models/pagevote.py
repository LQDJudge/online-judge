from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

from judge.models.profile import Profile
from judge.caching import cache_wrapper

__all__ = ["PageVote", "PageVoteVoter"]


class PageVote(models.Model):
    page = models.CharField(
        max_length=30,
        verbose_name=_("associated page"),
        db_index=True,
    )  # deprecated
    score = models.IntegerField(verbose_name=_("votes"), default=0)
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    linked_object = GenericForeignKey("content_type", "object_id")

    class Meta:
        verbose_name = _("pagevote")
        verbose_name_plural = _("pagevotes")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]
        unique_together = ("content_type", "object_id")

    @cache_wrapper(prefix="PVvs")
    def vote_score(self, user):
        page_vote = PageVoteVoter.objects.filter(pagevote=self, voter=user)
        if page_vote.exists():
            return page_vote.first().score
        else:
            return 0

    def __str__(self):
        return f"pagevote for {self.linked_object}"


class PageVoteVoter(models.Model):
    voter = models.ForeignKey(Profile, related_name="voted_page", on_delete=CASCADE)
    pagevote = models.ForeignKey(PageVote, related_name="votes", on_delete=CASCADE)
    score = models.IntegerField()

    class Meta:
        unique_together = ["voter", "pagevote"]
        verbose_name = _("pagevote vote")
        verbose_name_plural = _("pagevote votes")


class PageVotable:
    def get_or_create_pagevote(self):
        if self.pagevote.count():
            return self.pagevote.first()
        new_pagevote = PageVote()
        new_pagevote.linked_object = self
        new_pagevote.save()
        return new_pagevote
