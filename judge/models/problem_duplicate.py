from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class ProblemDuplicateReport(models.Model):
    PENDING = "P"
    SUCCESS = "S"
    FAILED = "F"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (SUCCESS, _("Success")),
        (FAILED, _("Failed")),
    ]

    min_score = models.FloatField(default=0.97)
    limit = models.PositiveIntegerField(default=100)
    neighbors = models.PositiveIntegerField(default=10)
    status = models.CharField(
        max_length=1, choices=STATUS_CHOICES, default=PENDING, db_index=True
    )
    task_id = models.CharField(max_length=64, blank=True, db_index=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    candidate_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["status", "-started_at"]),
            models.Index(fields=["min_score", "limit", "neighbors", "-started_at"]),
        ]

    def __str__(self):
        return f"Duplicate report #{self.id} ({self.get_status_display()})"


class ProblemDuplicateCandidate(models.Model):
    OPEN = "O"
    FALSE_POSITIVE = "F"
    STATUS_CHOICES = [
        (OPEN, _("Open")),
        (FALSE_POSITIVE, _("False positive")),
    ]

    report = models.ForeignKey(
        ProblemDuplicateReport,
        on_delete=models.CASCADE,
        related_name="candidates",
    )
    source_problem = models.ForeignKey(
        "Problem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    target_problem = models.ForeignKey(
        "Problem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    source_problem_id_snapshot = models.PositiveIntegerField()
    target_problem_id_snapshot = models.PositiveIntegerField()
    source_code = models.CharField(max_length=30)
    target_code = models.CharField(max_length=30)
    source_name = models.CharField(max_length=150)
    target_name = models.CharField(max_length=150)
    source_submission_count = models.PositiveIntegerField(default=0)
    target_submission_count = models.PositiveIntegerField(default=0)
    score = models.FloatField(db_index=True)
    status = models.CharField(
        max_length=1,
        choices=STATUS_CHOICES,
        default=OPEN,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-score", "source_code", "target_code"]
        indexes = [
            models.Index(fields=["report", "-score"]),
        ]

    def __str__(self):
        return f"{self.source_code} -> {self.target_code} ({self.score:.4f})"


class ProblemDuplicateMergeHistory(models.Model):
    PENDING = "P"
    RUNNING = "R"
    SUCCESS = "S"
    FAILED = "F"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (RUNNING, _("Running")),
        (SUCCESS, _("Success")),
        (FAILED, _("Failed")),
    ]

    source_problem = models.ForeignKey(
        "Problem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    target_problem = models.ForeignKey(
        "Problem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    source_problem_id_snapshot = models.PositiveIntegerField()
    target_problem_id_snapshot = models.PositiveIntegerField()
    source_code = models.CharField(max_length=30)
    target_code = models.CharField(max_length=30)
    source_name = models.CharField(max_length=150)
    target_name = models.CharField(max_length=150)
    merged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    status = models.CharField(
        max_length=1, choices=STATUS_CHOICES, default=PENDING, db_index=True
    )
    task_id = models.CharField(max_length=64, blank=True, db_index=True)
    requested_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    merged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error = models.TextField(blank=True)
    counts = models.JSONField(default=dict, blank=True)
    conflicts = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.source_code} -> {self.target_code}"


class ProblemDuplicateReviewHistory(models.Model):
    MERGE_QUEUED = "merge_queued"
    MERGE_RUNNING = "merge_running"
    MERGED = "merged"
    MERGE_FAILED = "merge_failed"
    MARKED_NOT_DUPLICATE = "marked_not_duplicate"
    ACTION_CHOICES = [
        (MERGE_QUEUED, _("Merge queued")),
        (MERGE_RUNNING, _("Merge running")),
        (MERGED, _("Merged")),
        (MERGE_FAILED, _("Merge failed")),
        (MARKED_NOT_DUPLICATE, _("Marked not duplicated")),
    ]

    action = models.CharField(max_length=32, choices=ACTION_CHOICES, db_index=True)
    source_code = models.CharField(max_length=30)
    target_code = models.CharField(max_length=30)
    source_problem_id_snapshot = models.PositiveIntegerField(null=True, blank=True)
    target_problem_id_snapshot = models.PositiveIntegerField(null=True, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_action_display()}: {self.source_code} -> {self.target_code}"
