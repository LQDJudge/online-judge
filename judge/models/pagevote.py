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
        page_vote = PageVoteVoter.objects.filter(pagevote=self, voter=user).first()
        return page_vote.score if page_vote else 0

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


@cache_wrapper(prefix="gocp")
def _get_or_create_pagevote(content_type, object_id):
    pagevote, created = PageVote.objects.get_or_create(
        content_type=content_type,
        object_id=object_id,
    )
    return pagevote


class PageVotable:
    def get_or_create_pagevote(self):
        content_type = ContentType.objects.get_for_model(self)
        object_id = self.pk
        return _get_or_create_pagevote(content_type, object_id)


def dirty_pagevote(pagevote, profile):
    pagevote.vote_score.dirty(pagevote, profile)
    _get_or_create_pagevote.dirty(pagevote.content_type, pagevote.object_id)
