import uuid

from django.db import models
from django.utils.translation import gettext_lazy as _


class ReviewEvidenceJob(models.Model):
    """Protected, snapshot-bound judge work; never an ordinary scored submission."""

    job_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    problem_run = models.ForeignKey(
        "ProblemReviewRun", on_delete=models.CASCADE, related_name="evidence_jobs"
    )
    operation = models.CharField(max_length=32)
    attempt_number = models.PositiveIntegerField(default=1)
    attempt_history = models.JSONField(default=list, blank=True)
    snapshot_hash = models.CharField(max_length=64)
    payload_hash = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=1,
        default="P",
        choices=(
            ("P", _("Pending")),
            ("R", _("Running")),
            ("D", _("Done")),
            ("E", _("Infrastructure error")),
            ("C", _("Cancelled")),
        ),
    )
    result = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    judge_name = models.CharField(max_length=100, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    started = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("problem_run", "operation", "payload_hash"),
                name="review_evidence_job_identity",
            )
        ]
