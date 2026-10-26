from django.db import models
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _


class PublicRequest(models.Model):
    PENDING = "P"
    APPROVED = "A"
    REJECTED = "R"
    WITHDRAWN = "W"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (APPROVED, _("Approved")),
        (REJECTED, _("Rejected")),
        (WITHDRAWN, _("Withdrawn")),
    ]

    problem = models.OneToOneField(
        "Problem",
        on_delete=models.CASCADE,
        related_name="public_request",
        verbose_name=_("problem"),
    )
    requested_by = models.ForeignKey(
        "judge.Profile",
        on_delete=models.CASCADE,
        related_name="public_requests",
        verbose_name=_("requested by"),
    )
    status = models.CharField(
        max_length=1,
        choices=STATUS_CHOICES,
        default=PENDING,
        verbose_name=_("status"),
        db_index=True,
    )
    feedback = models.TextField(
        blank=True,
        verbose_name=_("admin feedback"),
    )
    review_run = models.ForeignKey(
        "judge.ProblemReviewRun",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="publication_requests",
    )
    candidate_hash = models.CharField(max_length=64, blank=True)
    override_reason = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        "judge.Profile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_public_requests",
        verbose_name=_("reviewed by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("updated at"))

    class Meta:
        verbose_name = _("public request")
        verbose_name_plural = _("public requests")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.problem.code} - {self.get_status_display()}"


class PublicationRequestEvent(models.Model):
    """Append-only candidate/decision history behind legacy one-to-one requests."""

    problem = models.ForeignKey(
        "judge.Problem", null=True, blank=True, on_delete=models.CASCADE
    )
    contest = models.ForeignKey(
        "judge.Contest", null=True, blank=True, on_delete=models.CASCADE
    )
    problem_run = models.ForeignKey(
        "judge.ProblemReviewRun", null=True, blank=True, on_delete=models.PROTECT
    )
    contest_run = models.ForeignKey(
        "judge.ContestReviewRun", null=True, blank=True, on_delete=models.PROTECT
    )
    actor = models.ForeignKey("judge.Profile", null=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=1)
    candidate_hash = models.CharField(max_length=64, blank=True)
    reason = models.TextField(blank=True)
    readiness = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(_("Publication history is immutable."))
        return super().save(*args, **kwargs)

    class Meta:
        ordering = ["-created_at", "-pk"]
