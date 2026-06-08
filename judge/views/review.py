"""Dashboard for the auto-review pipeline."""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from django.db.models import F, Q
from django.http import HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import ListView

from judge.models import Comment, Problem, Profile
from judge.models.comment import (
    get_visible_comment_count,
    get_visible_top_level_comment_count,
)
from judge.models.problem_review import (
    ProblemReviewCheckResult,
    ProblemReviewRun,
)
from judge.models.public_request import PublicRequest
from judge.review.registry import CHECKS
from judge.review.triggers import trigger_problem_review_for
from judge.review.verdict import batched_verdicts
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.views import QueryStringSortMixin, TitleMixin
from judge.views.comment.forms import CommentForm
from judge.views.comment.mixins import is_comment_locked
from judge.views.comment.utils import parse_sort_params


def problem_review_dashboard(request, problem):
    """Render the review dashboard for a problem. Editors + superusers only."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden()
    problem_obj = get_object_or_404(Problem, code=problem)
    if not problem_obj.is_editable_by(request.user):
        return HttpResponseForbidden()

    # All runs for this problem, oldest first. We compute per-run index from
    # this list so callers never need a per-row COUNT query.
    all_runs = list(
        ProblemReviewRun.objects.filter(problem=problem_obj).order_by("started_at")
    )
    run_indices = {r.id: i + 1 for i, r in enumerate(all_runs)}

    latest = None
    for r in reversed(all_runs):
        if r.superseded_by_id is None:
            latest = r
            break

    # Optional ?run=<index> lets a user view an older run read-only. Index is
    # the per-problem 1-indexed sequence number (matches the dropdown label
    # "Run #N") — NOT the global DB id. Per-problem indexing makes URLs both
    # human-readable and stable across DB restores. Falls back to latest if
    # the param is missing, out of range, or non-numeric.
    selected = latest
    raw_run = request.GET.get("run")
    if raw_run:
        try:
            requested_index = int(raw_run)
            if 1 <= requested_index <= len(all_runs):
                selected = all_runs[requested_index - 1]
        except ValueError:
            pass

    selected_run_index = run_indices.get(selected.id) if selected else None
    viewing_history = selected is not None and selected.id != (
        latest.id if latest else None
    )

    # History list for the dropdown — newest first, includes everything
    # including the latest run for a stable "Switch to: Run #N" experience.
    history_entries = [
        {
            "id": r.id,
            "index": run_indices[r.id],
            "status": r.status,
            "status_display": r.get_status_display(),
            "started_at": r.started_at,
            "is_latest": (latest is not None and r.id == latest.id),
        }
        for r in reversed(all_runs)
    ]

    # Map check_id ("solutions_rubric") → friendly display_name ("Solutions
    # rubric") sourced from the registry. The template renders display_name
    # for users; the machine id stays in the DB row for stable lookups.
    check_display_names = {c.id: c.display_name for c in CHECKS}

    context = {
        "problem": problem_obj,
        "title": problem_obj.name + " — Review",
        "latest_run": selected,
        "latest_run_index": selected_run_index,
        "actual_latest_run": latest,
        "actual_latest_run_index": run_indices.get(latest.id) if latest else None,
        "viewing_history": viewing_history,
        "check_results": list(selected.check_results.all()) if selected else [],
        "check_display_names": check_display_names,
        "history_entries": history_entries,
    }

    # Comments are anchored to the FIRST run for this problem so the
    # discussion thread is continuous across re-runs. Authors and admins
    # see the full iteration history (every prior round's feedback, every
    # author response) on every dashboard view, instead of losing it when
    # a new run supersedes the old one. New comments posted from any
    # run's dashboard target the same anchor.
    anchor = (
        ProblemReviewRun.objects.filter(problem=problem_obj)
        .order_by("started_at")
        .first()
    )
    if anchor is not None:
        _attach_comment_context(request, context, anchor)

    return render(request, "problem/review.html", context)


def _attach_comment_context(request, context, target):
    """Wire up the comment system to `target` (a ProblemReviewRun)."""
    content_type = ContentType.objects.get_for_model(target)
    object_id = target.pk

    total = get_visible_comment_count(content_type.id, object_id)
    top_level = get_visible_top_level_comment_count(content_type.id, object_id)

    sort_by, sort_order = parse_sort_params(request)

    target_comment = -1
    raw_target = request.GET.get("target_comment")
    if raw_target:
        try:
            comment_obj = Comment.objects.get(id=int(raw_target))
            if (
                comment_obj.content_type_id == content_type.id
                and comment_obj.object_id == object_id
            ):
                target_comment = comment_obj.id
        except (ValueError, Comment.DoesNotExist):
            pass

    if request.user.is_authenticated:
        context["is_new_user"] = (
            not request.user.is_staff
            and not request.profile.submission_set.filter(
                points=F("problem__points")
            ).exists()
        )

    context.update(
        {
            "comment_lock": is_comment_locked(request),
            "has_comments": top_level > 0,
            "all_comment_count": total,
            "comment_content_type_id": content_type.id,
            "comment_object_id": object_id,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "target_comment": target_comment,
            "comment_form": CommentForm(request, initial={"parent": None}),
        }
    )


def problem_review_status(request, problem):
    """JSON endpoint used by dashboard polling."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "auth required"}, status=403)
    problem_obj = get_object_or_404(Problem, code=problem)
    if not problem_obj.is_editable_by(request.user):
        return JsonResponse({"error": "permission denied"}, status=403)

    latest = (
        ProblemReviewRun.objects.filter(problem=problem_obj, superseded_by__isnull=True)
        .order_by("-started_at")
        .first()
    )
    if not latest:
        return JsonResponse({"run": None})

    return JsonResponse(
        {
            "run": {
                "id": latest.id,
                "status": latest.status,
                "started_at": latest.started_at.isoformat(),
                "finished_at": (
                    latest.finished_at.isoformat() if latest.finished_at else None
                ),
                "summary_report": latest.summary_report,
                "check_results": [
                    {
                        "check_id": c.check_id,
                        "status": c.status,
                        "reason": c.reason,
                        "details_json": c.details_json,
                    }
                    for c in latest.check_results.all()
                ],
            }
        }
    )


@require_POST
def problem_review_rerun(request, problem):
    """Admin Rerun — force a fresh problem review, no guards.

    Restricted to superusers because it bypasses ALL guards (dirty-check,
    cooldown, in-flight). For authors, the "Request public" button is the
    supported way to trigger a review and it enforces guards via
    `judge.views.internal.request_public`.

    Does NOT touch PublicRequest — admin can dry-run a review on a problem
    that's never had a public-request. The trigger_problem_review_for helper
    handles the supersede-prior-runs step inside an atomic block.
    """
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden()
    problem_obj = get_object_or_404(Problem, code=problem)

    with transaction.atomic():
        trigger_problem_review_for(problem_obj, request.profile, dispatch="celery")

    return HttpResponseRedirect(
        reverse("problem_review_dashboard", args=[problem_obj.code])
    )


# ----------------------------------------------------------------------------
# Review list page — /problems/review/
# ----------------------------------------------------------------------------

VERDICT_FILTER_CHOICES = ("pass", "fail", "running", "error")
PUBLIC_REQUEST_FILTER_CHOICES = ("pending", "approved", "rejected", "none")


class ProblemReviewListView(QueryStringSortMixin, TitleMixin, ListView):
    """List of problems with at least one auto-review run.

    Two surfaces depending on permissions:
      - Admin / `edit_all_problem` perm: every problem with a review.
      - Anyone else: items they author or curate (testers can't edit, so
        they're excluded — same scope as `Problem.is_editable_by`).

    Each row links to `/problem/<code>/review` (the per-item dashboard).
    Used to track outstanding review work without having to remember each
    problem code individually.
    """

    paginate_by = 50
    template_name = "problem/review_list.html"
    context_object_name = "items"
    paginator_class = DiggPaginator

    # Sortable columns. `last_reviewed` is computed via a subquery annotation
    # below — the rest are direct DB fields. Default: newest review first.
    all_sorts = frozenset(("name", "last_reviewed", "public_status"))
    default_sort = "-last_reviewed"
    default_desc = frozenset(("last_reviewed",))

    def get_title(self):
        return _("Problem reviews")

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return Problem.objects.none()

        # `.prefetch_related("authors")` avoids one query per row when the
        # template renders `{% for author in p.authors.all() %}`. With a
        # 50-row page that's 50 → 1 query.
        qs = Problem.objects.filter(review_runs__isnull=False).prefetch_related(
            "authors__user"
        )

        # Permission scope mirrors `Problem.is_editable_by`: superuser OR the
        # global `edit_all_problem` perm, otherwise author/curator only.
        if not (
            self.request.user.is_superuser
            or self.request.user.has_perm("judge.edit_all_problem")
        ):
            pid = self.request.profile.id
            qs = qs.filter(Q(authors__id=pid) | Q(curators__id=pid))

        # Search — mirrors the /problems search logic in
        # `judge.views.problem.ProblemList.get_normal_queryset`. Substring
        # match on code, name, and the current language's translated name.
        # If FTS is enabled, union the FTS-ranked results so word-boundary
        # matches surface even when not a literal substring.
        search = " ".join(self.request.GET.getlist("search")).strip()
        if search:
            substr_qs = qs.filter(
                Q(code__icontains=search)
                | Q(name__icontains=search)
                | Q(
                    translations__name__icontains=search,
                    translations__language=self.request.LANGUAGE_CODE,
                )
            )
            if settings.ENABLE_FTS:
                qs = (
                    qs.search(search, qs.BOOLEAN).extra(order_by=["-relevance"])
                    | substr_qs
                )
            else:
                qs = substr_qs

        # Author filter (multi-select). Authors selected in the sidebar
        # narrow to problems where ANY of those authors is on the author list.
        author_ids = self._selected_author_ids()
        if author_ids:
            qs = qs.filter(authors__id__in=author_ids)

        public = self.request.GET.get("public")
        if public == "pending":
            qs = qs.filter(public_request__status=PublicRequest.PENDING)
        elif public == "approved":
            qs = qs.filter(public_request__status=PublicRequest.APPROVED)
        elif public == "rejected":
            qs = qs.filter(public_request__status=PublicRequest.REJECTED)
        elif public == "none":
            qs = qs.filter(public_request__isnull=True)

        verdict = self.request.GET.get("verdict")
        if verdict in VERDICT_FILTER_CHOICES:
            candidate_ids = list(qs.values_list("id", flat=True).distinct())
            _latest, verdicts = batched_verdicts(
                candidate_ids,
                ProblemReviewRun,
                ProblemReviewCheckResult,
                "problem_id",
            )
            matching = [iid for iid, v in verdicts.items() if v == verdict]
            qs = qs.filter(id__in=matching)

        # Annotate `last_reviewed` so DB-side sort works. Use Max on the
        # related run's started_at (covers RUNNING + DONE + ERROR runs).
        qs = qs.annotate(
            last_reviewed=models.Max("review_runs__started_at"),
        )

        order_field = self.order  # e.g. '-last_reviewed' / 'name'
        if order_field.lstrip("-") == "public_status":
            # Order by the OneToOne status string. NULL (no request) sorts
            # last regardless of direction by treating NULL as a sentinel.
            qs = qs.order_by(
                (
                    F("public_request__status").asc(nulls_last=True)
                    if not order_field.startswith("-")
                    else F("public_request__status").desc(nulls_last=True)
                ),
                "-id",
            )
        else:
            qs = qs.order_by(order_field, "-id")

        return qs.distinct()

    def _selected_author_ids(self):
        """Parse `?authors=ID&authors=ID` from the query string."""
        raw = self.request.GET.getlist("authors")
        out = []
        for r in raw:
            try:
                out.append(int(r))
            except (TypeError, ValueError):
                continue
        return out

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        items = list(context["items"])
        item_ids = [p.id for p in items]

        latest_runs, verdicts = batched_verdicts(
            item_ids, ProblemReviewRun, ProblemReviewCheckResult, "problem_id"
        )
        public_requests = {
            pr.problem_id: pr
            for pr in PublicRequest.objects.filter(problem_id__in=item_ids)
        }

        # Preload the selected author profiles so select2 can render their
        # chips on initial page load (it can't reach back into the AJAX
        # endpoint without an extra round-trip per chip).
        author_ids = self._selected_author_ids()
        selected_authors = list(
            Profile.objects.filter(id__in=author_ids).select_related("user")
        )

        context.update(
            {
                "latest_runs": latest_runs,
                "verdicts": verdicts,
                "public_requests": public_requests,
                "search_query": self.request.GET.get("search", ""),
                "active_verdict": self.request.GET.get("verdict", ""),
                "active_public": self.request.GET.get("public", ""),
                "selected_authors": selected_authors,
                "verdict_choices": VERDICT_FILTER_CHOICES,
                "public_choices": PUBLIC_REQUEST_FILTER_CHOICES,
                "page_type": "review",
                "first_page_href": self.request.path,
            }
        )
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context
