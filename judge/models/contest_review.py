"""Contest review runs and publication requests; phases/dependencies are shared."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from judge.models.review_coordinator import CoordinatorRunFields
from judge.models.contest import Contest


class ContestReviewRun(CoordinatorRunFields):
    RUNNING = "R"
    DONE = "D"
    ERROR = "E"
    BLOCKED = "B"
    CANCELLED = "C"
    STATUS_CHOICES = [
        (RUNNING, _("Running")),
        (DONE, _("Done")),
        (ERROR, _("Error")),
        (BLOCKED, _("Needs recovery")),
        (CANCELLED, _("Cancelled")),
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
        return list(
            Contest.authors.through.objects.filter(contest__review_runs__id=self.id)
            .values_list("profile_id", flat=True)
            .distinct()
        )


class ContestPublicRequest(models.Model):
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
    review_run = models.ForeignKey(
        "judge.ContestReviewRun",
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
