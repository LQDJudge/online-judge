from django.db import models, IntegrityError
from django.db.models import CASCADE, F
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

from judge.models.profile import Profile
from judge.caching import cache_wrapper

__all__ = ["PageVote", "PageVoteVoter", "PageVotable", "VoteService"]


class PageVote(models.Model):
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

    def vote_score(self, profile):
        voter_scores = get_voter_scores(self.id)
        return voter_scores.get(profile.id, 0)

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


@cache_wrapper(prefix="gocp", expected_type=PageVote)
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


@cache_wrapper(prefix="pvgvs")
def get_voter_scores(pagevote_id):
    page_votes = PageVoteVoter.objects.filter(pagevote=pagevote_id)
    return {pv.voter_id: pv.score for pv in page_votes}


def dirty_pagevote(pagevote):
    get_voter_scores.dirty(pagevote.id)
    _get_or_create_pagevote.dirty(pagevote.content_type, pagevote.object_id)


# Service layer to provide better abstraction
class VoteService:
    @staticmethod
    def vote(obj, user, value):
        """
        Apply a vote to an object

        Args:
            obj: Any PageVotable object
            user: User who is voting
            value: +1, 0, or -1
        """
        # Get the pagevote for this object
        pagevote = obj.get_or_create_pagevote()

        # Get or create voter record
        try:
            voter, created = PageVoteVoter.objects.get_or_create(
                pagevote=pagevote, voter=user.profile, defaults={"score": 0}
            )
        except IntegrityError:
            # Handle rare race condition
            voter = PageVoteVoter.objects.get(pagevote=pagevote, voter=user.profile)
            created = False

        # Calculate score change
        old_value = voter.score

        if value == 0:
            # Remove the vote
            if not created:
                PageVote.objects.filter(id=pagevote.id).update(
                    score=F("score") - old_value
                )
                voter.delete()
        else:
            # Update existing vote
            PageVote.objects.filter(id=pagevote.id).update(
                score=F("score") + value - old_value
            )
            voter.score = value
            voter.save()

        # Invalidate cache
        dirty_pagevote(pagevote)

        # Return updated score
        return PageVote.objects.get(id=pagevote.id).score

    @staticmethod
    def get_vote(obj, user):
        """Get a user's vote on an object"""
        if not user or not user.is_authenticated:
            return 0

        pagevote = obj.get_or_create_pagevote()
        return pagevote.vote_score(user.profile)
