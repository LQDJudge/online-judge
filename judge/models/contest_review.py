"""Auto-review models for the Contest publish flow.

Parallel to `judge/models/problem_review.py`. Three models live here:

  - ContestReviewRun           — one auto-review pass over a contest
  - ContestReviewCheckResult   — per-check verdict within a run
  - ContestPublicRequest       — author's request to make a private contest public

The two review models intentionally mirror their problem-review counterparts so the
existing dashboard / synthesis / reaper patterns can be ported with minimal change.
The check-result enum adds `WARNING` (advisory checks like difficulty/variety) on
top of the SUCCESS/FAIL/SKIPPED/ERROR set; the problem-review enum is left alone
so this addition doesn't ripple into already-shipped problem code.

ContestReviewRun also keeps an M2M to the ProblemReviewRun rows it consulted —
that powers the dashboard's "this contest review used review #N for problem X"
deep-links and lets us inspect why a problems_reviewed check passed/failed
without re-running anything.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class ContestReviewRun(models.Model):
    RUNNING = "R"
    DONE = "D"
    ERROR = "E"
    STATUS_CHOICES = [
        (RUNNING, _("Running")),
        (DONE, _("Done")),
        (ERROR, _("Error")),
    ]

    contest = models.ForeignKey(
        "judge.Contest",
        on_delete=models.CASCADE,
        related_name="review_runs",
        verbose_name=_("contest"),
    )
    triggered_by = models.ForeignKey(
        "judge.Profile",
        on_delete=models.SET_NULL,
        null=True,
        related_name="triggered_contest_review_runs",
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
        help_text=_("sha256 of contest state used by dirty-check"),
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
    # Which ProblemReviewRun rows this contest review consulted. Populated by
    # the problems_reviewed check. Lets the dashboard render "Problem X was
    # validated by review #N" links without re-querying.
    problem_review_runs = models.ManyToManyField(
        "judge.ProblemReviewRun",
        related_name="consulted_by_contest_reviews",
        blank=True,
        verbose_name=_("consulted problem review runs"),
    )
    # When True, the problems_reviewed check ALWAYS triggers a fresh
    # per-problem review (no hash reuse). Set by the admin "Rerun review"
    # button; cleared by the author "Request public" flow so unchanged
    # problems are skipped to save LLM/judge cycles.
    force_refresh_problems = models.BooleanField(
        default=False,
        verbose_name=_("force refresh per-problem reviews"),
    )

    class Meta:
        verbose_name = _("contest review run")
        verbose_name_plural = _("contest review runs")
        ordering = ["-started_at"]
        indexes = [
            models.Index(
                fields=["contest", "superseded_by", "-started_at"],
                name="con_review_run_latest_idx",
            ),
        ]

    def __str__(self):
        return (
            f"ContestReviewRun #{self.id} "
            f"({self.contest.key}, {self.get_status_display()})"
        )

    def get_author_ids(self):
        """Profiles considered "authors" of this run for permission checks.

        Required by the comment system's `get_content_author_ids` helper:
        every commentable content type must expose this. Semantically the
        authors of a contest review run are the contest's authors (the run
        is a snapshot of one request_public attempt). Without this method,
        the comment list endpoint asserts and returns 500 — visible to the
        user as "Tải bình luận bị lỗi" (Failed to load comments) in the
        dashboard's JS-rendered comment widget.
        """
        from judge.models import Contest

        return list(
            Contest.authors.through.objects.filter(contest__review_runs__id=self.id)
            .values_list("profile_id", flat=True)
            .distinct()
        )


class ContestReviewCheckResult(models.Model):
    PENDING = "P"
    SUCCESS = "S"
    FAIL = "F"
    WARNING = "W"
    SKIPPED = "K"
    ERROR = "E"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (SUCCESS, _("Pass")),
        (FAIL, _("Fail")),
        (WARNING, _("Warning")),
        (SKIPPED, _("Skipped")),
        (ERROR, _("Error")),
    ]

    run = models.ForeignKey(
        ContestReviewRun,
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
        verbose_name = _("contest review check result")
        verbose_name_plural = _("contest review check results")
        unique_together = [("run", "check_id")]
        ordering = ["run", "id"]

    def __str__(self):
        return f"{self.check_id}={self.get_status_display()} (run #{self.run_id})"


class ContestPublicRequest(models.Model):
    PENDING = "P"
    APPROVED = "A"
    REJECTED = "R"
    STATUS_CHOICES = [
        (PENDING, _("Pending")),
        (APPROVED, _("Approved")),
        (REJECTED, _("Rejected")),
    ]

    contest = models.OneToOneField(
        "judge.Contest",
        on_delete=models.CASCADE,
        related_name="public_request",
        verbose_name=_("contest"),
    )
    requested_by = models.ForeignKey(
        "judge.Profile",
        on_delete=models.CASCADE,
        related_name="contest_public_requests",
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
        related_name="reviewed_contest_public_requests",
        verbose_name=_("reviewed by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("updated at"))

    class Meta:
        verbose_name = _("contest public request")
        verbose_name_plural = _("contest public requests")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.contest.key} - {self.get_status_display()}"
