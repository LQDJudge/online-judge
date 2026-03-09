from django.db import models
from django.utils.translation import gettext_lazy as _


class PublicRequest(models.Model):
    PENDING = "P"
    APPROVED = "A"
    REJECTED = "R"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (APPROVED, _("Approved")),
        (REJECTED, _("Rejected")),
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
