from django.db import models
from django.db.models import CASCADE
from django.utils.translation import gettext_lazy as _

from judge.models import Profile, Problem, ProblemType

__all__ = ["VolunteerProblemVote"]


class VolunteerProblemVote(models.Model):
    voter = models.ForeignKey(
        Profile, related_name="volunteer_problem_votes", on_delete=CASCADE
    )
    problem = models.ForeignKey(
        Problem, related_name="volunteer_user_votes", on_delete=CASCADE
    )
    time = models.DateTimeField(auto_now_add=True)
    knowledge_points = models.PositiveIntegerField(
        verbose_name=_("knowledge points"),
        help_text=_("Points awarded by knowledge difficulty"),
    )
    thinking_points = models.PositiveIntegerField(
        verbose_name=_("thinking points"),
        help_text=_("Points awarded by thinking difficulty"),
    )
    types = models.ManyToManyField(
        ProblemType,
        verbose_name=_("problem types"),
        help_text=_("The type of problem, " "as shown on the problem's page."),
    )
    feedback = models.TextField(verbose_name=_("feedback"), blank=True)

    class Meta:
        verbose_name = _("volunteer vote")
        verbose_name_plural = _("volunteer votes")
        unique_together = ["voter", "problem"]

    def __str__(self):
        return f"{self.voter} for {self.problem.code}"
