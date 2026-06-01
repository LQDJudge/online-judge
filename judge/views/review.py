"""Dashboard for the auto-review pipeline."""

from django.contrib.contenttypes.models import ContentType
from django.db.models import F
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from judge.models import Comment, Problem, Submission
from judge.models.comment import (
    get_visible_comment_count,
    get_visible_top_level_comment_count,
)
from judge.models.problem_review import ProblemReviewRun, ProblemReviewSubmissionTag
from judge.review.registry import CHECKS
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
def problem_review_tag(request, problem):
    if not request.user.is_authenticated:
        return HttpResponseForbidden()
    problem_obj = get_object_or_404(Problem, code=problem)
    if not problem_obj.is_editable_by(request.user):
        return HttpResponseForbidden()

    try:
        submission_id = int(request.POST["submission_id"])
    except (KeyError, ValueError):
        return JsonResponse({"success": False, "error": "Missing submission_id"})

    try:
        submission = Submission.objects.get(id=submission_id, problem=problem_obj)
    except Submission.DoesNotExist:
        return JsonResponse({"success": False, "error": "Submission not found"})

    # kind is now optional — author may provide it as a hint, otherwise the LLM classifies.
    kind = request.POST.get("kind") or None
    if kind is not None and kind not in dict(ProblemReviewSubmissionTag.KIND_CHOICES):
        return JsonResponse({"success": False, "error": "Invalid kind"})

    target_subtask_raw = request.POST.get("target_subtask")
    target_subtask = None
    if target_subtask_raw:
        try:
            target_subtask = int(target_subtask_raw)
        except ValueError:
            target_subtask = None

    tag, created = ProblemReviewSubmissionTag.objects.update_or_create(
        submission=submission,
        defaults={
            "tagged_by": request.profile,
            "kind": kind,
            "target_subtask": target_subtask,
            "claimed_complexity": request.POST.get("claimed_complexity", "").strip(),
            "note": request.POST.get("note", "").strip(),
        },
    )
    return JsonResponse({"success": True, "tag_id": tag.id, "created": created})


@require_POST
def problem_review_untag(request, problem):
    if not request.user.is_authenticated:
        return HttpResponseForbidden()
    problem_obj = get_object_or_404(Problem, code=problem)
    if not problem_obj.is_editable_by(request.user):
        return HttpResponseForbidden()
    try:
        submission_id = int(request.POST["submission_id"])
    except (KeyError, ValueError):
        return JsonResponse({"success": False, "error": "Missing submission_id"})
    deleted_count, _ = ProblemReviewSubmissionTag.objects.filter(
        submission_id=submission_id,
        submission__problem=problem_obj,
    ).delete()
    return JsonResponse({"success": True, "deleted": deleted_count})
