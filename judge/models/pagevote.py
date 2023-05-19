from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _

from judge.models.profile import Profile

__all__ = ["PageVote", "PageVoteVoter"]


class PageVote(models.Model):
    page = models.CharField(
        max_length=30,
        verbose_name=_("associated page"),
        db_index=True,
    )
    score = models.IntegerField(verbose_name=_("votes"), default=0)

    class Meta:
        verbose_name = _("pagevote")
        verbose_name_plural = _("pagevotes")

    def vote_score(self, user):
        page_vote = PageVoteVoter.objects.filter(pagevote=self, voter=user)
        if page_vote.exists():
            return page_vote.first().score
        else:
            return 0

    def __str__(self):
        return f"pagevote for {self.page}"


class PageVoteVoter(models.Model):
    voter = models.ForeignKey(Profile, related_name="voted_page", on_delete=CASCADE)
    pagevote = models.ForeignKey(PageVote, related_name="votes", on_delete=CASCADE)
    score = models.IntegerField()

    class Meta:
        unique_together = ["voter", "pagevote"]
        verbose_name = _("pagevote vote")
        verbose_name_plural = _("pagevote votes")
