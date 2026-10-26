"""Durable coordinator state. Legacy run/check records remain intact."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class CoordinatorRunFields(models.Model):
    workflow_version = models.PositiveSmallIntegerField(default=0)
    content_hash = models.CharField(max_length=64, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)
    readiness = models.CharField(max_length=32, blank=True)
    current_stale = models.BooleanField(default=False)
    trigger_is_superuser = models.BooleanField(default=False)
    credit_consumed = models.BooleanField(default=False)
    credit_settled = models.BooleanField(default=False)
    notifications_sent = models.BooleanField(default=False)

    class Meta:
        abstract = True


class ReviewStage(models.Model):
    PENDING = "P"
    RUNNING = "R"
    DONE = "D"
    ERROR = "E"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (RUNNING, _("Running")),
        (DONE, _("Done")),
        (ERROR, _("Infrastructure failure")),
    ]
    problem_run = models.ForeignKey(
        "judge.ProblemReviewRun",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="coordinator_stages",
    )
    contest_run = models.ForeignKey(
        "judge.ContestReviewRun",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="coordinator_stages",
    )
    stage_id = models.CharField(max_length=64)
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default=PENDING)
    input_json = models.JSONField(default=dict, blank=True)
    request_json = models.JSONField(default=dict, blank=True)
    output_json = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    error_category = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    delivery_attempts = models.PositiveIntegerField(default=0)
    manual_retries = models.PositiveSmallIntegerField(default=0)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    retry_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("problem_run", "stage_id"), ("contest_run", "stage_id")]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(problem_run__isnull=False, contest_run__isnull=True)
                    | models.Q(problem_run__isnull=True, contest_run__isnull=False)
                ),
                name="review_stage_one_run",
            )
        ]

    @property
    def run(self):
        return self.problem_run if self.problem_run_id else self.contest_run

    @property
    def kind(self):
        return "problem" if self.problem_run_id else "contest"


class ReviewStageAttempt(models.Model):
    stage = models.ForeignKey(
        ReviewStage, on_delete=models.CASCADE, related_name="attempt_records"
    )
    number = models.PositiveIntegerField()
    raw_request = models.TextField(blank=True)
    raw_response = models.TextField(blank=True)
    error = models.TextField(blank=True)
    usage_json = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    diagnostics_expires_at = models.DateTimeField(null=True, blank=True)
    retained_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [("stage", "number")]


class ContestReviewDependency(models.Model):
    contest_run = models.ForeignKey(
        "judge.ContestReviewRun",
        on_delete=models.CASCADE,
        related_name="child_dependencies",
    )
    problem = models.ForeignKey("judge.Problem", on_delete=models.CASCADE)
    problem_run = models.ForeignKey(
        "judge.ProblemReviewRun",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="parent_dependencies",
    )
    content_hash = models.CharField(max_length=64)
    owned = models.BooleanField(default=False)
    selected = models.BooleanField(default=False)

    class Meta:
        unique_together = [("contest_run", "problem")]
