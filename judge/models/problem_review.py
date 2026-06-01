from django.db import models
from django.utils.translation import gettext_lazy as _

from judge.models.problem import Problem


class ProblemReviewRun(models.Model):
    RUNNING = "R"
    DONE = "D"
    ERROR = "E"
    STATUS_CHOICES = [
        (RUNNING, _("Running")),
        (DONE, _("Done")),
        (ERROR, _("Error")),
    ]

    problem = models.ForeignKey(
        "judge.Problem",
        on_delete=models.CASCADE,
        related_name="review_runs",
        verbose_name=_("problem"),
    )
    triggered_by = models.ForeignKey(
        "judge.Profile",
        on_delete=models.SET_NULL,
        null=True,
        related_name="triggered_review_runs",
        verbose_name=_("triggered by"),
    )
    status = models.CharField(
        max_length=1,
        choices=STATUS_CHOICES,
        default=RUNNING,
        db_index=True,
        verbose_name=_("status"),
    )
    input_hash = models.CharField(
        max_length=64,
        verbose_name=_("input hash"),
        help_text=_("sha256 of problem state used by dirty-check"),
    )
    started_at = models.DateTimeField(auto_now_add=True, verbose_name=_("started at"))
    finished_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("finished at")
    )
    summary_report = models.TextField(blank=True, verbose_name=_("summary report"))
    superseded_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="supersedes",
        verbose_name=_("superseded by"),
    )

    class Meta:
        verbose_name = _("problem review run")
        verbose_name_plural = _("problem review runs")
        ordering = ["-started_at"]
        indexes = [
            models.Index(
                fields=["problem", "superseded_by", "-started_at"],
                name="prob_review_run_latest_idx",
            ),
        ]

    def __str__(self):
        return f"ProblemReviewRun #{self.id} ({self.problem.code}, {self.get_status_display()})"

    def get_author_ids(self):
        """Profiles considered "authors" of this run for permission checks.

        Required by the comment system's `get_content_author_ids` helper:
        every commentable content type must expose this. Semantically the
        author of a review run is whoever authors the underlying problem
        (the run is just a snapshot of one request_public attempt).
        """
        return list(
            Problem.authors.through.objects.filter(problem__review_runs__id=self.id)
            .values_list("profile_id", flat=True)
            .distinct()
        )


class ProblemReviewCheckResult(models.Model):
    PENDING = "P"
    SUCCESS = "S"
    FAIL = "F"
    SKIPPED = "K"
    ERROR = "E"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (SUCCESS, _("Pass")),
        (FAIL, _("Fail")),
        (SKIPPED, _("Skipped")),
        (ERROR, _("Error")),
    ]

    run = models.ForeignKey(
        ProblemReviewRun,
        on_delete=models.CASCADE,
        related_name="check_results",
        verbose_name=_("review run"),
    )
    check_id = models.CharField(max_length=64, verbose_name=_("check id"))
    status = models.CharField(
        max_length=1,
        choices=STATUS_CHOICES,
        default=PENDING,
        verbose_name=_("status"),
    )
    reason = models.TextField(blank=True, verbose_name=_("reason"))
    details_json = models.JSONField(default=dict, blank=True, verbose_name=_("details"))
    started_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("started at")
    )
    finished_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("finished at")
    )

    class Meta:
        verbose_name = _("problem review check result")
        verbose_name_plural = _("problem review check results")
        unique_together = [("run", "check_id")]
        ordering = ["run", "id"]

    def __str__(self):
        return f"{self.check_id}={self.get_status_display()} (run #{self.run_id})"


class ProblemReviewSubmissionTag(models.Model):
    MAIN = "M"
    SUBTASK = "S"
    BRUTE_FORCE = "B"
    KIND_CHOICES = [
        (MAIN, _("Main AC")),
        (SUBTASK, _("Subtask-targeted")),
        (BRUTE_FORCE, _("Brute force / suboptimal")),
    ]

    submission = models.OneToOneField(
        "judge.Submission",
        on_delete=models.CASCADE,
        related_name="review_tag",
        verbose_name=_("submission"),
    )
    tagged_by = models.ForeignKey(
        "judge.Profile",
        on_delete=models.SET_NULL,
        null=True,
        related_name="review_tags",
        verbose_name=_("tagged by"),
    )
    kind = models.CharField(
        max_length=1,
        choices=KIND_CHOICES,
        null=True,
        blank=True,
        verbose_name=_("kind"),
        help_text=_("Optional — the LLM will classify if not set."),
    )
    target_subtask = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_("target subtask"),
        help_text=_("1-indexed. Required when kind=Subtask."),
    )
    claimed_complexity = models.CharField(
        max_length=64,
        blank=True,
        verbose_name=_("claimed complexity"),
    )
    note = models.TextField(blank=True, verbose_name=_("author note"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("updated at"))

    class Meta:
        verbose_name = _("problem review submission tag")
        verbose_name_plural = _("problem review submission tags")
        ordering = ["submission__problem", "kind", "target_subtask"]

    def __str__(self):
        if self.kind == self.SUBTASK:
            return f"Tag #{self.id}: submission {self.submission_id} → subtask {self.target_subtask}"
        return f"Tag #{self.id}: submission {self.submission_id} ({self.get_kind_display()})"
