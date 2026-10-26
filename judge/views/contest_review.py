"""Dashboard for the contest auto-review pipeline.

Mirrors `judge/views/review.py` for problems. Same shape: a server-rendered
template plus a JSON status endpoint the dashboard polls until status flips
from R to D/E.

Comment thread is anchored to the FIRST contest review run so discussion
spans across re-runs (same pattern as problem review).
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from django.db.models import F, Q
from django.http import HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.vary import vary_on_headers
from django.views.generic import ListView

from judge.models import Comment, Contest, Profile
from judge.models.comment import (
    get_visible_comment_count,
    get_visible_top_level_comment_count,
)
from judge.models.contest_review import (
    ContestPublicRequest,
    ContestReviewCheckResult,
    ContestReviewRun,
)
from judge.review.contest_hashing import compute_contest_input_hash
from judge.review.contest_registry import CONTEST_CHECKS
from judge.review.decisions import (
    accept_contest_public_request,
    reject_contest_public_request,
)
from judge.review.verdict import batched_verdicts
from judge.review.workflow import enabled, is_review_readable
from judge.review.publication import (
    submit_publication,
    withdraw_publication,
    decide_publication,
    publish_without_review,
)
from judge.utils.community import can_use_community_features
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.timefmt import format_mmss
from judge.utils.views import QueryStringSortMixin, TitleMixin
from judge.views.comment.forms import CommentForm
from judge.views.comment.mixins import is_comment_locked
from judge.views.comment.utils import parse_sort_params
from judge.views.coordinator_review import coordinator_context, run_review


def _contest_sidebar_context(request, contest_obj):
    """Populate the context vars `contest-tabs.html` expects.

    The contest sidebar normally gets these from `ContestMixin.get_context_data`,
    but our function-based view doesn't extend that mixin. Re-implementing the
    minimal subset here keeps the function-based pattern (cheap to read) while
    making the sidebar render correctly.
    """
    # Local imports avoid a circular dep at module import time: contests.py
    # imports things from this module's neighborhood via the URL router.
    from judge.views.contests import is_contest_clonable

    return {
        "now": timezone.now(),
        "can_access": contest_obj.is_accessible_by(request.user),
        "can_edit": contest_obj.is_editable_by(request.user),
        "can_view_editor": contest_obj.can_manage_activity_by(request.user),
        "can_manage_contest_activity": contest_obj.can_manage_activity_by(request.user),
        "has_moss_api_key": settings.MOSS_API_KEY is not None,
        "contest_has_hidden_subtasks": contest_obj.format.has_hidden_subtasks,
        "show_final_ranking": (
            (
                contest_obj.format.has_hidden_subtasks
                or contest_obj.contest_problems.filter(is_result_hidden=True).exists()
            )
            and contest_obj.can_manage_activity_by(request.user)
        ),
        "is_clonable": is_contest_clonable(request, contest_obj),
    }


@login_required
@never_cache
@vary_on_headers("X-Review-Live")
def contest_review_dashboard(request, contest):
    """Render the review dashboard for a contest. Editors + superusers only."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden()
    contest_obj = get_object_or_404(Contest, key=contest)
    if not is_review_readable(contest_obj, request.user):
        return HttpResponseForbidden()

    # All runs for this contest, oldest first. Per-contest 1-indexed sequence
    # numbers stay stable across DB restores; we use them in the history
    # dropdown label ("Run #N") rather than raw DB ids.
    all_runs = list(
        ContestReviewRun.objects.filter(contest=contest_obj).order_by("started_at")
    )
    run_indices = {r.id: i + 1 for i, r in enumerate(all_runs)}

    latest = None
    for r in reversed(all_runs):
        if r.superseded_by_id is None:
            latest = r
            break

    # ?run=<index> shows an older run read-only — same convention as the
    # problem-review dashboard.
    selected = latest
    raw_id = request.GET.get("run_id")
    if raw_id:
        try:
            selected = next((run for run in all_runs if run.pk == int(raw_id)), latest)
        except ValueError:
            pass
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

    check_display_names = {c.id: c.display_name for c in CONTEST_CHECKS}

    public_request = ContestPublicRequest.objects.filter(contest=contest_obj).first()

    context = {
        "contest": contest_obj,
        "title": contest_obj.name + " — Review",
        "latest_run": selected,
        "latest_run_index": selected_run_index,
        "actual_latest_run": latest,
        "actual_latest_run_index": run_indices.get(latest.id) if latest else None,
        "viewing_history": viewing_history,
        "check_results": list(selected.check_results.all()) if selected else [],
        "check_display_names": check_display_names,
        "history_entries": history_entries,
        "public_request": public_request,
    }
    context.update(_contest_sidebar_context(request, contest_obj))
    context.update(coordinator_context(contest_obj, selected, request.user))
    if public_request and public_request.review_run_id:
        context["coordinator_enabled"] = True
    if (
        request.headers.get("X-Review-Live") == "1"
        and selected
        and selected.workflow_version == 2
    ):
        context["coordinator_live"] = True
        return render(request, "review/coordinator_dashboard.html", context)

    # Anchor comments to the FIRST run so the discussion thread persists
    # across re-runs (same rationale as problem review).
    anchor = (
        ContestReviewRun.objects.filter(contest=contest_obj)
        .order_by("started_at")
        .first()
    )
    if anchor is not None:
        _attach_comment_context(request, context, anchor)

    return render(request, "contest/review.html", context)


def _attach_comment_context(request, context, target):
    """Wire up the comment system to `target` (a ContestReviewRun)."""
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
        context["is_new_user"] = not can_use_community_features(
            request.user, request.profile
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


@require_POST
def contest_review_trigger(request, contest):
    """Admin Rerun — force a fresh contest review, no guards.

    Restricted to superusers because it bypasses ALL guards (dirty-check,
    cooldown, in-flight). For non-admins, the "Request public contest"
    flow on the contest edit page is the supported way to create a review,
    and it enforces guards via `contest_request_public`.

    Honors the `AUTO_REVIEW_CONTEST_ENABLED` ops kill switch — when False,
    new reviews refuse to start; existing in-flight runs continue.
    """
    if enabled():
        return run_review(request, "contest", contest)
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden()
    if not getattr(settings, "AUTO_REVIEW_CONTEST_ENABLED", True):
        return HttpResponseRedirect(reverse("contest_review_dashboard", args=[contest]))
    contest_obj = get_object_or_404(Contest, key=contest)

    # Imports kept local to the trigger path to avoid pulling the Celery
    # task module into every dashboard render.
    from judge.models.contest_review import ContestReviewRun
    from judge.review.contest_hashing import compute_contest_input_hash
    from judge.tasks.contest_review import review_contest

    with transaction.atomic():
        # Create the new run, then mark all prior non-superseded runs for
        # this contest as superseded by it. The order matters: we need the
        # new run's id before we can point superseded_by at it.
        # `force_refresh_problems=True` — admin Rerun means "rebuild
        # everything from scratch", including per-problem reviews even when
        # their input_hash hasn't changed.
        run = ContestReviewRun.objects.create(
            contest=contest_obj,
            triggered_by=request.profile,
            input_hash=compute_contest_input_hash(contest_obj),
            force_refresh_problems=True,
        )
        ContestReviewRun.objects.filter(
            contest=contest_obj, superseded_by__isnull=True
        ).exclude(id=run.id).update(superseded_by=run)

    review_contest.delay(run.id)
    return HttpResponseRedirect(
        reverse("contest_review_dashboard", args=[contest_obj.key])
    )


@never_cache
@vary_on_headers("X-Review-Progress")
def contest_review_status(request, contest):
    """JSON endpoint used by dashboard polling."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "auth required"}, status=403)
    contest_obj = get_object_or_404(Contest, key=contest)
    if not is_review_readable(contest_obj, request.user):
        return JsonResponse({"error": "permission denied"}, status=403)

    latest = (
        ContestReviewRun.objects.filter(contest=contest_obj, superseded_by__isnull=True)
        .order_by("-started_at")
        .first()
    )
    if not latest:
        return JsonResponse({"run": None})

    if request.headers.get("X-Review-Progress") == "1" and latest.workflow_version == 2:
        return JsonResponse(
            {
                "run": {
                    "id": latest.id,
                    "status": latest.status,
                    "coordinator_stages": list(
                        latest.coordinator_stages.order_by("id").values(
                            "stage_id", "status"
                        )
                    ),
                }
            }
        )

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
                "readiness": latest.readiness,
                "workflow_version": latest.workflow_version,
                "coordinator_stages": (
                    list(
                        latest.coordinator_stages.order_by("id").values(
                            "stage_id", "status", "reason", "output_json"
                        )
                    )
                    if latest.workflow_version == 2
                    else []
                ),
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
def contest_request_public(request, contest):
    """Author requests a private contest to be made public.

    The coordinator path requests publication of an exact reviewed candidate;
    it never starts another review. The legacy path below retains its original
    review-triggering guards while the coordinator feature is disabled.
    """
    if not request.user.is_authenticated:
        return HttpResponseForbidden()

    contest_obj = get_object_or_404(Contest, key=contest)

    # Guard 1: Permission.
    if (
        enabled()
        or ContestPublicRequest.objects.filter(
            contest=contest_obj, review_run__isnull=False
        ).exists()
    ):
        try:
            run_id = request.POST.get("run_id")
            public_request = submit_publication(
                contest_obj, request.user, int(run_id) if run_id else None
            )
        except (ValidationError, ValueError) as exc:
            return JsonResponse(
                {
                    "success": False,
                    "error": (
                        "; ".join(exc.messages)
                        if isinstance(exc, ValidationError)
                        else _("Invalid review run.")
                    ),
                },
                status=400,
            )
        return JsonResponse(
            {
                "success": True,
                "request_id": public_request.pk,
                "redirect": reverse("contest_review_dashboard", args=[contest]),
            }
        )

    if not contest_obj.can_request_public_by(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"})

    # Ops kill switch — when False, new public requests refuse. Existing
    # in-flight runs continue (we don't try to cancel them; the reaper will
    # eventually time them out if they truly hang).
    if not getattr(settings, "AUTO_REVIEW_CONTEST_ENABLED", True):
        return JsonResponse(
            {
                "success": False,
                "error": _("Contest auto-review is currently disabled."),
            }
        )

    # Imports kept local — same rationale as contest_review_trigger.
    from judge.tasks.contest_review import review_contest

    new_hash = compute_contest_input_hash(contest_obj)

    # SELECT … FOR UPDATE on the Contest row serializes concurrent POSTs.
    # Same anti-double-create pattern problem review uses.
    with transaction.atomic():
        Contest.objects.select_for_update().filter(id=contest_obj.id).first()

        # Guard 2: In-flight.
        if ContestReviewRun.objects.filter(
            contest=contest_obj, status=ContestReviewRun.RUNNING
        ).exists():
            return JsonResponse(
                {
                    "success": False,
                    "error": _("Review currently running, please wait."),
                }
            )

        latest_run = (
            ContestReviewRun.objects.filter(contest=contest_obj)
            .order_by("-started_at")
            .first()
        )

        if latest_run is not None:
            # Guard 3: Dirty-check — admins bypass so they can re-run a
            # review without having to edit the contest first (useful for
            # diagnosing flaky LLM checks or testing a config change).
            if latest_run.input_hash == new_hash and not request.user.is_superuser:
                return JsonResponse(
                    {
                        "success": False,
                        "error": _(
                            "No changes since your last review — edit "
                            "something and try again."
                        ),
                    }
                )
            # Guard 4: Cooldown — only for the same non-admin user re-requesting.
            same_user_recently = (
                not request.user.is_superuser
                and latest_run.triggered_by_id == request.profile.id
            )
            if same_user_recently:
                cooldown_seconds = getattr(
                    settings, "AUTO_REVIEW_CONTEST_REQUEST_COOLDOWN_SECONDS", 300
                )
                cooldown_end = latest_run.started_at + timedelta(
                    seconds=cooldown_seconds
                )
                remaining = (cooldown_end - timezone.now()).total_seconds()
                if remaining > 0:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": _(
                                "Please wait %(mmss)s before requesting review again."
                            )
                            % {"mmss": format_mmss(remaining)},
                            "cooldown_seconds_remaining": int(remaining),
                        }
                    )

        # All guards passed. Refresh or create the ContestPublicRequest row.
        existing = ContestPublicRequest.objects.filter(contest=contest_obj).first()
        if existing:
            existing.status = ContestPublicRequest.PENDING
            existing.requested_by = request.profile
            existing.feedback = ""
            existing.reviewed_by = None
            existing.save(
                update_fields=[
                    "status",
                    "requested_by",
                    "feedback",
                    "reviewed_by",
                    "updated_at",
                ]
            )
        else:
            ContestPublicRequest.objects.create(
                contest=contest_obj,
                requested_by=request.profile,
            )

        new_run = ContestReviewRun.objects.create(
            contest=contest_obj,
            triggered_by=request.profile,
            input_hash=new_hash,
        )
        # Bulk-supersede ALL prior non-superseded runs, not just `latest_run`.
        # Defensive: if a previous bug or race ever left two parallel "head"
        # runs, this collapses them under the new one rather than leaving an
        # inconsistent dashboard. Same pattern as `contest_review_trigger`
        # and `trigger_problem_review_for`.
        ContestReviewRun.objects.filter(
            contest=contest_obj, superseded_by__isnull=True
        ).exclude(id=new_run.id).update(superseded_by=new_run)

        # transaction.on_commit so the Celery dispatch only happens after
        # the DB row is durably visible to the worker.
        transaction.on_commit(lambda: review_contest.delay(new_run.id))

    return JsonResponse(
        {
            "success": True,
            "run_id": new_run.id,
            "redirect": reverse("contest_review_dashboard", args=[contest_obj.key]),
        }
    )


@require_POST
def contest_request_cancel(request, contest):
    """Author withdraws their PENDING ContestPublicRequest.

    Mirrors `cancel_request_public` for problems. Deletes the PENDING row
    so admins stop seeing it in the queue. ContestReviewRun rows the
    request triggered remain (review history, not publish-request state).
    """
    if not request.user.is_authenticated:
        return HttpResponseForbidden()
    contest_obj = get_object_or_404(Contest, key=contest)
    if (
        enabled()
        or ContestPublicRequest.objects.filter(
            contest=contest_obj, review_run__isnull=False
        ).exists()
    ):
        withdraw_publication(contest_obj, request.user)
        return JsonResponse({"success": True})
    if not contest_obj.can_request_public_by(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"})
    deleted, _ignored = ContestPublicRequest.objects.filter(
        contest=contest_obj, status=ContestPublicRequest.PENDING
    ).delete()
    return JsonResponse({"success": True, "cancelled": deleted})


@require_POST
def contest_review_accept(request, contest):
    """Admin records an Accept verdict on the contest's public request.

    Status-only — does NOT publish (is_visible untouched). Posts a system
    comment + notifies the author via the shared decisions service.
    """
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden()
    contest_obj = get_object_or_404(Contest, key=contest)
    feedback = request.POST.get("feedback", "").strip()
    if (
        enabled()
        or ContestPublicRequest.objects.filter(
            contest=contest_obj, review_run__isnull=False
        ).exists()
    ):
        try:
            with transaction.atomic():
                contest_obj = Contest.objects.select_for_update().get(pk=contest_obj.pk)
                pending = ContestPublicRequest.objects.filter(
                    contest=contest_obj, status=ContestPublicRequest.PENDING
                ).first()
                if pending and pending.review_run_id:
                    decide_publication(
                        contest_obj,
                        request.user,
                        feedback=feedback,
                        override_reason=request.POST.get("override_reason", ""),
                        publish=True,
                    )
                else:
                    publish_without_review(
                        contest_obj,
                        request.user,
                        request.POST.get("override_reason", ""),
                    )
        except ValidationError as exc:
            return JsonResponse(
                {"success": False, "error": "; ".join(exc.messages)}, status=400
            )
        return JsonResponse({"success": True})
    pr = accept_contest_public_request(contest_obj, request.profile, feedback)
    if pr is None:
        return JsonResponse(
            {"success": False, "error": _("No public request to act on.")}
        )
    return JsonResponse({"success": True})


@require_POST
def contest_review_reject(request, contest):
    """Admin records a Reject verdict on the contest's public request.

    Status-only — does NOT unpublish (is_visible untouched). Posts a system
    comment + notifies the author via the shared decisions service.
    """
    if not request.user.is_authenticated or not request.user.is_superuser:
        return HttpResponseForbidden()
    contest_obj = get_object_or_404(Contest, key=contest)
    feedback = request.POST.get("feedback", "").strip()
    if not feedback:
        return JsonResponse(
            {
                "success": False,
                "error": _("Please explain why this publication request is rejected."),
            },
            status=400,
        )
    if (
        enabled()
        or ContestPublicRequest.objects.filter(
            contest=contest_obj, review_run__isnull=False
        ).exists()
    ):
        try:
            decide_publication(
                contest_obj,
                request.user,
                approve=False,
                feedback=feedback,
                expected_request_token=request.POST.get("request_token"),
            )
        except ValidationError as exc:
            return JsonResponse(
                {"success": False, "error": "; ".join(exc.messages)}, status=400
            )
        return JsonResponse(
            {
                "success": True,
                "redirect": reverse("contest_review_dashboard", args=[contest_obj.key])
                + "?tab=discussion",
            }
        )
    pr = reject_contest_public_request(contest_obj, request.profile, feedback)
    if pr is None:
        return JsonResponse(
            {"success": False, "error": _("No public request to act on.")}
        )
    return JsonResponse({"success": True})


# ----------------------------------------------------------------------------
# Review list page — /contests/review/
# ----------------------------------------------------------------------------

VERDICT_FILTER_CHOICES = (
    "pass",
    "warning",
    "fail",
    "manual",
    "incomplete",
    "stale",
    "cancelled",
    "running",
    "error",
)
PUBLIC_REQUEST_FILTER_CHOICES = ("pending", "approved", "rejected", "withdrawn", "none")


class ContestReviewListView(
    LoginRequiredMixin, QueryStringSortMixin, TitleMixin, ListView
):
    """List of contests with at least one auto-review run.

    Mirrors `judge.views.review.ProblemReviewListView`. Permission scope
    matches `Contest.is_editable_by`: superusers + holders of
    `judge.edit_all_contest` see all; otherwise authors/curators only.

    Superusers land on pending publication requests; authors see all their
    reviews. Explicit query-string filters are preserved.

    Each row links to `/contest/<key>/review` (the per-item dashboard).
    """

    paginate_by = 50
    template_name = "contest/review_list.html"
    context_object_name = "items"
    paginator_class = DiggPaginator

    all_sorts = frozenset(("name", "last_reviewed", "public_status"))
    default_sort = "-last_reviewed"
    default_desc = frozenset(("last_reviewed",))

    def get(self, request, *args, **kwargs):
        if request.user.is_superuser and not request.GET:
            return HttpResponseRedirect(request.path + "?public=pending")
        return super().get(request, *args, **kwargs)

    def get_title(self):
        return _("Contest reviews")

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return Contest.objects.none()

        # `.prefetch_related("authors")` avoids one query per row when the
        # template renders `{% for author in c.authors.all() %}` — same fix
        # the reviewer flagged on the problem-side list.
        qs = Contest.objects.filter(review_runs__isnull=False).prefetch_related(
            "authors__user"
        )
        if not enabled():
            qs = qs.filter(
                Q(start_time__gt=timezone.now())
                | Q(is_visible=False)
                | Q(is_organization_private=True)
                | Q(is_private=True)
                | Q(is_in_course=True)
            )

        if not (
            self.request.user.is_superuser
            or self.request.user.has_perm("judge.edit_all_contest")
        ):
            pid = self.request.profile.id
            qs = qs.filter(Q(authors__id=pid) | Q(curators__id=pid))

        # Search — mirrors `judge.views.contests.ContestList.setup_contest_list`
        # (substring on key/name plus an FTS-ranked branch when ENABLE_FTS).
        search = " ".join(self.request.GET.getlist("search")).strip()
        if search:
            substr_qs = qs.filter(Q(key__icontains=search) | Q(name__icontains=search))
            if settings.ENABLE_FTS:
                qs = qs.search(search).extra(order_by=["-relevance"]) | substr_qs
            else:
                qs = substr_qs

        author_ids = self._selected_author_ids()
        if author_ids:
            qs = qs.filter(authors__id__in=author_ids)

        # Match the problem dashboard: site-public content is approved even
        # when it was published through a different superuser workflow. Keep
        # the historical publication-request row untouched.
        qs = qs.annotate(
            dashboard_public_status=models.Case(
                models.When(
                    is_visible=True,
                    is_private=False,
                    is_organization_private=False,
                    is_in_course=False,
                    then=models.Value(ContestPublicRequest.APPROVED),
                ),
                default=F("public_request__status"),
                output_field=models.CharField(),
            )
        )
        public = self.request.GET.get("public")
        if public == "pending":
            qs = qs.filter(dashboard_public_status=ContestPublicRequest.PENDING)
        elif public == "approved":
            qs = qs.filter(dashboard_public_status=ContestPublicRequest.APPROVED)
        elif public == "rejected":
            qs = qs.filter(dashboard_public_status=ContestPublicRequest.REJECTED)
        elif public == "withdrawn":
            qs = qs.filter(dashboard_public_status=ContestPublicRequest.WITHDRAWN)
        elif public == "none":
            qs = qs.filter(dashboard_public_status__isnull=True)

        verdict = self.request.GET.get("verdict")
        if verdict in VERDICT_FILTER_CHOICES:
            candidate_ids = list(qs.values_list("id", flat=True).distinct())
            _latest, verdicts = batched_verdicts(
                candidate_ids,
                ContestReviewRun,
                ContestReviewCheckResult,
                "contest_id",
            )
            matching = [iid for iid, v in verdicts.items() if v == verdict]
            qs = qs.filter(id__in=matching)

        qs = qs.annotate(last_reviewed=models.Max("review_runs__started_at"))

        order_field = self.order
        if order_field.lstrip("-") == "public_status":
            qs = qs.order_by(
                (
                    F("dashboard_public_status").asc(nulls_last=True)
                    if not order_field.startswith("-")
                    else F("dashboard_public_status").desc(nulls_last=True)
                ),
                "-id",
            )
        else:
            qs = qs.order_by(order_field, "-id")

        return qs.distinct()

    def _selected_author_ids(self):
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
        item_ids = [c.id for c in items]

        latest_runs, verdicts = batched_verdicts(
            item_ids, ContestReviewRun, ContestReviewCheckResult, "contest_id"
        )
        public_requests = {
            pr.contest_id: pr
            for pr in ContestPublicRequest.objects.filter(contest_id__in=item_ids)
        }

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
                "now": timezone.now(),
            }
        )
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context
