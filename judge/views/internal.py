import difflib
import logging
import math
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.db.models import Q
from django.http import (
    Http404,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _, get_language
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView, View

from chat_box.models import ChatModerationLog
from chat_box.utils import encrypt_channel
from chat_box.views import hide_lobby_message, mute_chat_user
from judge import event_poster as event
from judge.blog_composer.cache import get_session
from judge.judgeapi import bridge_status
from judge.ml.problem_duplicates import (
    DuplicateProblemMergePending,
    DuplicateProblemReportOptions,
    DuplicateProblemReportRefreshPending,
    create_pending_duplicate_problem_merge,
    get_cached_duplicate_problem_candidates,
    get_done_duplicate_problem_merges,
    get_duplicate_problem_merge_history,
    get_duplicate_problem_report_refresh_state,
    get_pending_duplicate_problem_merges,
    mark_duplicate_candidate_false_positive,
    schedule_duplicate_problem_report_refresh,
)
from judge.ml.semantic_search import (
    SemanticSearchUnavailable,
)
from judge.models import (
    BlogPost,
    CommentModerationLog,
    Organization,
    Problem,
    ProblemType,
    Profile,
    RequestMetric,
    Submission,
    UsernameModerationCase,
    get_comment_context_details,
    hide_comment_for_moderation,
    mute_comment_author,
)
from judge.models.notification import Notification, NotificationCategory
from judge.models.problem import get_distinct_problem_points
from judge.models.public_request import PublicRequest
from judge.models.problem_review import ProblemReviewCheckResult, ProblemReviewRun
from judge.review.hashing import compute_input_hash
from judge.review.system_bot import post_system_comment_on_review
from judge.review.triggers import trigger_problem_review_for
from judge.review.verdict import batched_verdicts
from judge.tasks import rescore_problem
from judge.tasks.llm import improve_markdown_task, tag_problem_task
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.problem_equivalence import (
    ProblemEquivalenceError,
    ProblemEquivalenceVerifier,
)
from judge.utils.problem_merge import ProblemMerge
from judge.utils.strings import safe_float_or_none
from judge.utils.timefmt import format_mmss

logger = logging.getLogger(__name__)


class InternalView(object):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if request.user.is_superuser:
            return super(InternalView, self).dispatch(request, *args, **kwargs)
        return HttpResponseForbidden()

    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return super(InternalView, self).get(request, *args, **kwargs)
        return HttpResponseForbidden()


class InternalProblemDuplicates(InternalView, TemplateView):
    title = _("Duplicate Problems")
    template_name = "internal/problem_duplicates.html"

    def dispatch(self, request, *args, **kwargs):
        if not getattr(settings, "USE_ML", False):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") == "false_positive":
            source_code = request.POST.get("source")
            target_code = request.POST.get("target")
            updated = mark_duplicate_candidate_false_positive(
                source_code,
                target_code,
                user=request.user,
            )
            if updated:
                messages.success(
                    request,
                    _("Marked %(source)s and %(target)s as not duplicated.")
                    % {"source": source_code, "target": target_code},
                )
            else:
                messages.warning(
                    request,
                    _(
                        "No open duplicate candidate was found for %(source)s and %(target)s."
                    )
                    % {"source": source_code, "target": target_code},
                )
            return redirect("internal_problem_duplicates")

        options = self._options_from_request(request.POST)
        try:
            schedule_duplicate_problem_report_refresh(
                options, requested_by=request.user
            )
        except SemanticSearchUnavailable as exc:
            messages.error(request, str(exc))
        except DuplicateProblemReportRefreshPending:
            messages.error(request, _("A duplicate report refresh is already pending."))
        else:
            messages.success(request, _("Duplicate report refresh queued."))
        return redirect("internal_problem_duplicates")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        options = self._options_from_request(self.request.GET)
        current_tab = self.request.GET.get("tab", "report")
        candidates = get_cached_duplicate_problem_candidates(options)
        refresh_state = get_duplicate_problem_report_refresh_state()
        error = None
        if not getattr(settings, "USE_ML", False):
            error = _("USE_ML is disabled")
        context["page_type"] = "problem_duplicates"
        context["title"] = self.title
        context["current_tab"] = current_tab
        context["candidates"] = candidates or []
        context["has_cached_report"] = candidates is not None
        context["merge_history"] = get_duplicate_problem_merge_history()
        context["pending_merges"] = get_pending_duplicate_problem_merges()
        context["done_merges"] = get_done_duplicate_problem_merges()
        context["refresh_state"] = refresh_state
        context["refresh_pending"] = refresh_state.get("status") == "PENDING"
        context["error"] = error
        context["min_score"] = options.min_score
        context["limit"] = options.limit
        context["neighbors"] = options.neighbors
        return context

    def _options_from_request(self, params):
        return DuplicateProblemReportOptions(
            min_score=self._safe_float(params.get("min_score"), 0.97, 0.5, 1.0),
            limit=self._safe_int(params.get("limit"), 100, 1, 500),
            neighbors=self._safe_int(params.get("neighbors"), 10, 1, 50),
        )

    def _safe_int(self, value, default, min_value, max_value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        return max(min_value, min(value, max_value))

    def _safe_float(self, value, default, min_value, max_value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = default
        return max(min_value, min(value, max_value))


class InternalProblemDuplicateDetail(InternalView, TemplateView):
    title = _("Duplicate Problem Review")
    template_name = "internal/problem_duplicate_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        source, target = self._get_merge_pair()
        verification_ids = self._verification_ids_from_request()
        context["page_type"] = "problem_duplicates"
        context["title"] = self.title
        context["source"] = source
        context["target"] = target
        context["is_reverse_merge_direction"] = source.id < target.id
        context["swap_merge_url"] = self._swap_merge_url(source, target)
        context["merge_report"] = self._merge_dry_run(source, target)
        context["statement_diff"] = self._statement_diff(source, target)
        context["source_ac_submissions"] = self._accepted_submissions(source)
        context["target_ac_submissions"] = self._accepted_submissions(target)
        context["verification_submissions"] = self._verification_submissions(
            verification_ids
        )
        context["verification_ids"] = ",".join(map(str, verification_ids))
        return context

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")
        source, target = self._get_merge_pair()
        if action == "verify":
            return self._post_verify(request, source, target)
        if action == "merge":
            return self._post_merge(request, source, target)
        if action == "false_positive":
            return self._post_false_positive(request, source, target)
        raise Http404()

    def _get_merge_pair(self):
        source_code = self.request.GET.get("source") or self.request.POST.get("source")
        target_code = self.request.GET.get("target") or self.request.POST.get("target")
        if not source_code or not target_code:
            raise Http404()
        try:
            first = Problem.objects.get(code=source_code)
            second = Problem.objects.get(code=target_code)
        except Problem.DoesNotExist as exc:
            raise Http404() from exc
        if first.id == second.id:
            raise Http404()
        larger, smaller = (first, second) if first.id > second.id else (second, first)
        if self._is_reverse_direction():
            return smaller, larger
        return larger, smaller

    def _merge_dry_run(self, source, target):
        try:
            return ProblemMerge(
                source.code,
                target.code,
                force=source.id < target.id,
            ).run()
        except Exception as exc:
            logger.warning(
                "Failed to build duplicate merge dry-run for %s -> %s: %s",
                source.code,
                target.code,
                exc,
                exc_info=True,
            )
            return {"error": str(exc)}

    def _is_reverse_direction(self):
        return (
            self.request.GET.get("direction") == "reverse"
            or self.request.POST.get("direction") == "reverse"
        )

    def _swap_merge_url(self, source, target):
        url = "%s?source=%s&target=%s" % (
            reverse("internal_problem_duplicate_detail"),
            source.code,
            target.code,
        )
        if source.id > target.id:
            url += "&direction=reverse"
        return url

    def _statement_diff(self, source, target):
        source_text = source.description or ""
        target_text = target.description or ""
        diff_lines = list(
            difflib.unified_diff(
                target_text.splitlines(),
                source_text.splitlines(),
                lineterm="",
                n=2,
            )
        )
        return {
            "old": target_text,
            "new": source_text,
            "diff_lines": [
                line
                for line in diff_lines
                if not line.startswith("---") and not line.startswith("+++")
            ],
        }

    def _accepted_submissions(self, problem, limit=10):
        return (
            Submission.objects.filter(
                problem=problem,
                status="D",
                result="AC",
                source__isnull=False,
            )
            .select_related("user__user", "language", "source")
            .order_by("-case_points", "-points", "-date", "-id")[:limit]
        )

    def _verification_ids_from_request(self):
        raw_ids = self.request.GET.get("verification_ids", "")
        ids = []
        for raw_id in raw_ids.split(","):
            try:
                ids.append(int(raw_id))
            except ValueError:
                continue
        return ids[:20]

    def _verification_submissions(self, ids):
        if not ids:
            return []
        submissions = Submission.objects.filter(id__in=ids).select_related(
            "problem", "user__user", "language"
        )
        submission_map = {submission.id: submission for submission in submissions}
        return [
            submission_map[submission_id]
            for submission_id in ids
            if submission_id in submission_map
        ]

    def _post_verify(self, request, source, target):
        verify_source_code = request.POST.get("verify_source")
        verify_target_code = request.POST.get("verify_target")
        count = self._safe_int(request.POST.get("count"), 3, 1, 5)
        try:
            verify_source = Problem.objects.get(code=verify_source_code)
            verify_target = Problem.objects.get(code=verify_target_code)
        except Problem.DoesNotExist:
            messages.error(request, _("Problem not found."))
            return self._redirect_to_detail(source, target)

        submissions = self._accepted_submissions(verify_source, limit=count)
        if not submissions:
            messages.error(
                request,
                _("No accepted source submissions with stored source code were found."),
            )
            return self._redirect_to_detail(source, target)

        verification_ids = []
        for submission in submissions:
            try:
                report = ProblemEquivalenceVerifier(
                    verify_source.code,
                    verify_target.code,
                    source_submission_id=submission.id,
                    apply=True,
                ).run()
            except ProblemEquivalenceError as exc:
                messages.error(request, str(exc))
                continue
            verification_ids.append(report["verification_submission_id"])

        if verification_ids:
            messages.success(
                request,
                _("Queued %(count)s verification submissions.")
                % {"count": len(verification_ids)},
            )
        return self._redirect_to_detail(source, target, verification_ids)

    def _post_merge(self, request, source, target):
        if request.POST.get("confirm") != "MERGE":
            messages.error(request, _("Type MERGE to confirm the merge."))
            return self._redirect_to_detail(source, target)

        task_id = str(uuid.uuid4())
        try:
            merge = create_pending_duplicate_problem_merge(
                source,
                target,
                user=request.user,
                task_id=task_id,
                force=source.id < target.id,
            )
        except DuplicateProblemMergePending:
            messages.error(request, _("A merge for these problems is already pending."))
            return redirect(
                "%s?tab=pending_merges" % reverse("internal_problem_duplicates")
            )

        from judge.tasks.semantic_search import merge_duplicate_problem

        merge_duplicate_problem.apply_async((merge.id,), task_id=task_id)
        messages.success(
            request,
            _("Merge queued for %(source)s into %(target)s.")
            % {"source": source.code, "target": target.code},
        )
        return redirect("internal_problem_duplicates")

    def _post_false_positive(self, request, source, target):
        candidate_source, candidate_target = (
            (source, target) if source.id > target.id else (target, source)
        )
        mark_duplicate_candidate_false_positive(
            candidate_source.code,
            candidate_target.code,
            user=request.user,
        )
        messages.success(
            request,
            _("Marked %(source)s and %(target)s as not duplicated.")
            % {"source": candidate_source.code, "target": candidate_target.code},
        )
        return redirect("internal_problem_duplicates")

    def _redirect_to_detail(self, source, target, verification_ids=None):
        url = "%s?source=%s&target=%s" % (
            reverse("internal_problem_duplicate_detail"),
            source.code,
            target.code,
        )
        if source.id < target.id:
            url += "&direction=reverse"
        if verification_ids:
            url += "&verification_ids=%s" % ",".join(map(str, verification_ids))
        return redirect(url)

    def _safe_int(self, value, default, min_value, max_value):
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        return max(min_value, min(value, max_value))


class InternalProblemDuplicateStatusApi(InternalView, View):
    def get(self, request, *args, **kwargs):
        ids = []
        for raw_id in request.GET.get("ids", "").split(","):
            try:
                ids.append(int(raw_id))
            except ValueError:
                continue
        submissions = Submission.objects.filter(id__in=ids).select_related(
            "problem", "language"
        )
        submission_map = {submission.id: submission for submission in submissions}
        results = []
        for submission_id in ids:
            submission = submission_map.get(submission_id)
            if not submission:
                continue
            results.append(
                {
                    "id": submission.id,
                    "problem": submission.problem.code,
                    "language": submission.language.key,
                    "status": submission.status,
                    "result": submission.result,
                    "points": submission.points,
                    "case_points": submission.case_points,
                    "case_total": submission.case_total,
                    "passed": submission.status == "D"
                    and submission.result == "AC"
                    and (
                        not submission.case_total
                        or submission.case_points == submission.case_total
                    ),
                }
            )
        return JsonResponse({"submissions": results})


class InternalBridgeStatus(InternalView, TemplateView):
    title = _("Bridge Status")
    template_name = "internal/bridge_status.html"
    queue_limit = 100

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "bridge_status"
        context["title"] = self.title
        context["bridge_error"] = None
        context["status"] = {}

        try:
            status = bridge_status(detail=True, include_problems=False)
            if status.get("name") != "bridge-status":
                raise ValueError(_("Malformed bridge status response."))
        except Exception as exc:
            logger.warning("Failed to load bridge status: %s", exc, exc_info=True)
            context["bridge_error"] = _("Bridge status is unavailable: %(error)s") % {
                "error": str(exc),
            }
            return context

        judges = []
        for judge in status.get("judges-detail", []):
            judge = dict(judge)
            judge.pop("problems", None)
            judge["load-display"] = self._format_number(judge.get("load"))
            judge["latency-display"] = self._format_number(judge.get("latency"))
            judge["address-display"] = self._format_address(judge.get("address"))
            judge["client-address-display"] = self._format_address(
                judge.get("client-address")
            )
            if judge["client-address-display"] == judge["address-display"]:
                judge["client-address-display"] = None
            judges.append(judge)

        queue = status.get("queue", [])
        displayed_queue = queue[: self.queue_limit]
        active_submissions = status.get("active-submissions-detail", [])
        running_users = status.get("running-users", [])
        user_display = self._profile_user_display(
            running_users,
            active_submissions,
            displayed_queue,
        )

        context["status"] = status
        context["queue"] = self._with_user_display(displayed_queue, user_display)
        context["queue_total"] = len(queue)
        context["queue_limit"] = self.queue_limit
        active_submissions = self._with_user_display(
            active_submissions,
            user_display,
        )
        submission_user_display = {
            item["submission-id"]: item["user-username"] for item in active_submissions
        }
        for judge in judges:
            judge["current-submission-user-username"] = submission_user_display.get(
                judge.get("current-submission")
            )
        context["judges"] = judges
        context["active_submissions"] = active_submissions
        context["active_validations"] = status.get("active-validations-detail", [])
        context["running_users"] = [
            {
                "id": user_id,
                "display": user_display.get(user_id, "#%s" % user_id),
                "username": user_display.get(user_id),
            }
            for user_id in running_users
        ]
        return context

    def _profile_user_display(self, running_users, *item_groups):
        user_ids = {user_id for user_id in running_users if user_id is not None}
        for items in item_groups:
            for item in items:
                user_id = item.get("user-id")
                if user_id is not None:
                    user_ids.add(user_id)

        profiles = Profile.get_cached_instances(*sorted(user_ids))
        return {profile.id: profile.username for profile in profiles}

    def _with_user_display(self, items, user_display):
        enriched = []
        for item in items:
            item = dict(item)
            user_id = item.get("user-id")
            username = user_display.get(user_id)
            item["user-username"] = username
            item["user-display"] = (
                username or "#%s" % user_id if user_id is not None else None
            )
            enriched.append(item)
        return enriched

    def _format_number(self, value):
        if value is None:
            return None
        try:
            return ("%.3f" % float(value)).rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return value

    def _format_address(self, value):
        if value is None:
            return None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return "%s:%s" % (value[0], value[1])
        if not isinstance(value, str):
            return value
        if value.startswith("[") and "]:" in value:
            host, port = value[1:].split("]:", 1)
            if ":" not in host:
                return "%s:%s" % (host, port)
        return value


class InternalProblemQueue(InternalView, ListView):
    model = Problem
    title = _("Internal problem queue")
    template_name = "internal/problem_queue.html"
    paginate_by = 20
    context_object_name = "problems"

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        return DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs,
        )

    def setup_problem_filter(self, request):
        """Setup filter parameters similar to ProblemList.setup_problem_list"""
        self.search_query = None
        self.author_query = []
        self.point_start = safe_float_or_none(request.GET.get("point_start"))
        self.point_end = safe_float_or_none(request.GET.get("point_end"))
        self.current_tab = request.GET.get("tab", "public")
        self.status_filter = request.GET.get("status", "")
        # Auto-review verdict filter — accept only the two verdicts we expose
        # in the UI; anything else (empty/junk) means "no verdict filter".
        verdict = request.GET.get("verdict", "")
        self.verdict_filter = verdict if verdict in ("pass", "fail") else ""

        # Handle author filter
        if "authors" in request.GET:
            try:
                self.author_query = list(map(int, request.GET.getlist("authors")))
            except ValueError:
                pass

    def get_queryset(self):
        """Enhanced queryset with filtering similar to ProblemList.get_normal_queryset"""
        # Setup filters
        self.setup_problem_filter(self.request)

        if self.current_tab == "request_public":
            return self._get_request_public_queryset()
        return self._get_public_queue_queryset()

    def _get_public_queue_queryset(self):
        """Original public queue: public, non-org-private problems."""
        queryset = Problem.objects.filter(is_public=True, is_organization_private=False)
        queryset = self._apply_search_filters(queryset)
        return queryset.distinct().order_by("-id")

    def _get_request_public_queryset(self):
        """Request public queue: problems with a PublicRequest.

        The Pending sub-filter is the actionable work list, so there we hide
        problems that are already site-public (public AND not
        organization-private) — they need no further action, whether they were
        published via approval here or through some other path (admin edit,
        contest publish, etc.) that left a stale pending request behind. The
        other sub-filters (All / Approved / Rejected) keep the full record for
        reference.
        """
        queryset = Problem.objects.filter(public_request__isnull=False).select_related(
            "public_request",
            "public_request__requested_by",
            "public_request__reviewed_by",
        )

        if self.status_filter:
            queryset = queryset.filter(public_request__status=self.status_filter)

        if self.status_filter == PublicRequest.PENDING:
            queryset = queryset.exclude(is_public=True, is_organization_private=False)

        queryset = self._apply_search_filters(queryset)

        # Auto-review verdict is a derived value (no DB column), so we can't
        # express it as a plain .filter(). Resolve the candidate ids first,
        # compute verdicts in 2 batched queries, then narrow by id. Only runs
        # when a verdict is actually selected. Mirrors ProblemReviewListView.
        if self.verdict_filter:
            candidate_ids = list(queryset.values_list("id", flat=True).distinct())
            _latest, verdicts = batched_verdicts(
                candidate_ids,
                ProblemReviewRun,
                ProblemReviewCheckResult,
                "problem_id",
            )
            matching = [pid for pid, v in verdicts.items() if v == self.verdict_filter]
            queryset = queryset.filter(id__in=matching)

        return queryset.distinct().order_by("-public_request__created_at")

    def _apply_search_filters(self, queryset):
        """Apply common search/author/point filters."""
        if "search" in self.request.GET:
            self.search_query = query = " ".join(
                self.request.GET.getlist("search")
            ).strip()
            if query:
                substr_queryset = queryset.filter(
                    Q(code__icontains=query)
                    | Q(name__icontains=query)
                    | Q(
                        translations__name__icontains=query,
                        translations__language=get_language(),
                    )
                )
                if settings.ENABLE_FTS:
                    queryset = (
                        queryset.search(query, queryset.BOOLEAN).extra(
                            order_by=["-relevance"]
                        )
                        | substr_queryset
                    )
                else:
                    queryset = substr_queryset

        if self.author_query:
            queryset = queryset.filter(authors__in=self.author_query)

        if self.point_start is not None:
            queryset = queryset.filter(points__gte=self.point_start)
        if self.point_end is not None:
            queryset = queryset.filter(points__lte=self.point_end)

        return queryset

    def get_noui_slider_points(self):
        """Get point range data for slider (same logic as ProblemList)"""
        points = get_distinct_problem_points()
        if not points:
            return 0, 0, {}
        if len(points) == 1:
            return (
                points[0],
                points[0],
                {
                    "min": points[0] - 1,
                    "max": points[0] + 1,
                },
            )

        start, end = points[0], points[-1]
        if self.point_start is not None:
            start = self.point_start
        if self.point_end is not None:
            end = self.point_end
        points_map = {0.0: "min", 1.0: "max"}
        size = len(points) - 1
        return (
            start,
            end,
            {
                points_map.get(i / size, "%.2f%%" % (100 * i / size,)): j
                for i, j in enumerate(points)
            },
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "problem_queue"
        context["title"] = self.title
        context["current_tab"] = self.current_tab
        context["status_filter"] = self.status_filter
        context["verdict_filter"] = self.verdict_filter

        # Add filter context data
        context["search_query"] = getattr(self, "search_query", None)
        context["author_query"] = Profile.objects.filter(
            id__in=getattr(self, "author_query", [])
        )

        # Point range context
        (
            context["point_start"],
            context["point_end"],
            context["point_values"],
        ) = self.get_noui_slider_points()

        # Request counts for tab badges. Mirror the request_public queue's
        # exclusion of already-site-public problems so the badge matches the
        # number of rows actually shown under the Pending filter.
        context["pending_request_count"] = (
            PublicRequest.objects.filter(status=PublicRequest.PENDING)
            .exclude(
                problem__is_public=True,
                problem__is_organization_private=False,
            )
            .count()
        )

        # Build pagination URLs that preserve filter parameters
        query_params = self.request.GET.copy()
        if "page" in query_params:
            del query_params["page"]

        if query_params:
            query_string = query_params.urlencode()
            context["page_prefix"] = self.request.path + "?" + query_string + "&page="
            context["first_page_href"] = self.request.path + "?" + query_string
        else:
            context["page_prefix"] = self.request.path + "?page="
            context["first_page_href"] = self.request.path

        # Auto-review status for the request_public tab.
        if self.current_tab == "request_public":
            problems_in_page = list(context.get("problems", []))
            problem_ids = [p.id for p in problems_in_page]
            latest_runs, verdicts = batched_verdicts(
                problem_ids,
                ProblemReviewRun,
                ProblemReviewCheckResult,
                "problem_id",
            )
            context["latest_review_runs"] = latest_runs
            # Ensure every problem on the page has a key (even if no run).
            context["review_verdicts"] = {
                p.id: verdicts.get(p.id) for p in problems_in_page
            }
        else:
            context["latest_review_runs"] = {}
            context["review_verdicts"] = {}

        return context


class InternalCommunityBlogQueue(InternalView, ListView):
    model = BlogPost
    title = _("Community blog review queue")
    template_name = "internal/community_blog_queue.html"
    paginate_by = 20
    context_object_name = "posts"

    def get_queryset(self):
        self.current_tab = self.request.GET.get("tab", "pending")
        if self.current_tab not in ("pending", "composer"):
            self.current_tab = "pending"
        if self.current_tab == "composer":
            return BlogPost.objects.none()
        self.search_query = self.request.GET.get("search", "").strip()
        self.sort_order = self.request.GET.get("sort", "oldest")
        if self.sort_order not in ("oldest", "newest"):
            self.sort_order = "oldest"
        queryset = (
            BlogPost.objects.filter(
                organizations__is_community=True,
                visible=False,
                is_rejected=False,
            )
            .prefetch_related("authors", "organizations")
            .distinct()
        )
        if self.search_query:
            queryset = queryset.filter(
                Q(title__icontains=self.search_query)
                | Q(content__icontains=self.search_query)
                | Q(organizations__name__icontains=self.search_query)
                | Q(organizations__slug__icontains=self.search_query)
            ).distinct()
        if self.sort_order == "newest":
            return queryset.order_by("-publish_on", "-id")
        return queryset.order_by("publish_on", "id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        posts = list(context["posts"])
        for post in posts:
            community_orgs = [
                org for org in post.organizations.all() if org.is_community
            ]
            post.review_organization = community_orgs[0] if community_orgs else None
            post.review_organizations = community_orgs
        context["posts"] = posts
        context["title"] = self.title
        context["page_type"] = "community_blog_queue"
        context["current_tab"] = self.current_tab
        context["is_admin"] = True
        context["is_moderator"] = True
        context["hide_texts_on_mobile"] = False
        context["show_organization_tags"] = True
        context["search_query"] = getattr(self, "search_query", "")
        context["sort_order"] = getattr(self, "sort_order", "oldest")
        context["pending_count"] = (
            BlogPost.objects.filter(
                organizations__is_community=True,
                visible=False,
                is_rejected=False,
            )
            .distinct()
            .count()
        )
        context["community_count"] = (
            Organization.objects.filter(
                is_community=True,
                blogpost__visible=False,
                blogpost__is_rejected=False,
            )
            .distinct()
            .count()
        )
        if self.current_tab == "composer":
            try:
                composer_post_id = int(self.request.GET.get("post", "") or 0) or None
            except ValueError:
                composer_post_id = None
            context["composer_post"] = (
                BlogPost.objects.filter(
                    id=composer_post_id, organizations__is_community=True
                )
                .distinct()
                .first()
                if composer_post_id
                else None
            )
            context["composer_session"] = get_session(
                self.request.user.id, composer_post_id
            )
            context["composer_organizations"] = Organization.objects.filter(
                is_community=True
            ).order_by("name")
            context["composer_default_author"] = getattr(
                settings, "MAGAZINE_AUTHOR_USERNAME", "admin"
            )
        return context


@require_POST
def mark_problem_private(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    try:
        problem_id = int(request.POST.get("id"))
        problem = Problem.objects.get(id=problem_id)
    except Exception:
        return HttpResponseForbidden()

    problem.is_public = False
    problem.save()
    return JsonResponse({"success": True})


@require_POST
def publish_problem(request):
    """Publish a problem: set is_public=True, clear orgs, mark request as approved."""
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    try:
        problem_id = int(request.POST.get("id"))
        problem = Problem.objects.get(id=problem_id)
    except Exception:
        return JsonResponse({"success": False, "error": "Problem not found"})

    with transaction.atomic():
        problem.is_public = True
        problem.is_organization_private = False
        problem._bypass_points_cap = True
        problem.save(update_fields=["is_public", "is_organization_private"])
        problem.organizations.clear()

        # Update the public request status
        try:
            pr = problem.public_request
            pr.status = PublicRequest.APPROVED
            pr.reviewed_by = request.profile
            pr.save(update_fields=["status", "reviewed_by", "updated_at"])
        except PublicRequest.DoesNotExist:
            pass

        # Rescore after publish
        transaction.on_commit(lambda: rescore_problem.delay(problem.id))

    # Notify the author (notification bell)
    _notify_request_author(problem, request.profile, PublicRequest.APPROVED)

    # Post a system message in the review thread so the conversation is
    # complete — author sees "approved" inline with their iteration history
    # instead of only via the notification bell. The reviewer name is wrapped
    # in `[user:NAME]` (the project's reference-filter syntax) so it renders
    # as a rank-colored link just like "Triggered by ..." in the run header.
    reviewer_token = "[user:%s]" % request.user.username
    post_system_comment_on_review(
        problem,
        _("**[System]** This problem was approved by %(name)s. It is now public.")
        % {"name": reviewer_token},
    )

    return JsonResponse({"success": True})


@require_POST
def reject_problem(request):
    """Reject a public request with feedback."""
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    try:
        problem_id = int(request.POST.get("id"))
        problem = Problem.objects.get(id=problem_id)
    except Exception:
        return JsonResponse({"success": False, "error": "Problem not found"})

    feedback = request.POST.get("feedback", "").strip()

    try:
        pr = problem.public_request
        pr.status = PublicRequest.REJECTED
        pr.feedback = feedback
        pr.reviewed_by = request.profile
        pr.save(update_fields=["status", "feedback", "reviewed_by", "updated_at"])
    except PublicRequest.DoesNotExist:
        return JsonResponse({"success": False, "error": "No public request found"})

    # Notify the author (notification bell)
    _notify_request_author(problem, request.profile, PublicRequest.REJECTED)

    # Post a system message in the review thread so the author sees the
    # rejection reason inline with their iteration history. Include the
    # feedback verbatim (it's the actionable instruction they need). The
    # reviewer name uses `[user:NAME]` so the reference filter renders it
    # as a rank-colored link (matching the header style elsewhere).
    reviewer_token = "[user:%s]" % request.user.username
    if feedback:
        body = _(
            "**[System]** This problem was rejected by %(name)s.\n\n**Reason:** %(reason)s"
        ) % {"name": reviewer_token, "reason": feedback}
    else:
        body = _(
            "**[System]** This problem was rejected by %(name)s. No reason was provided."
        ) % {"name": reviewer_token}
    post_system_comment_on_review(problem, body)

    return JsonResponse({"success": True})


def improve_markdown_queue(request):
    """Handle improve markdown from the queue page."""
    if not request.user.is_superuser:
        return HttpResponseForbidden()

    if request.method == "GET":
        problem_code = request.GET.get("problem_code")
        if not problem_code:
            return JsonResponse({"success": False, "error": "Problem code is required"})

        try:
            problem = Problem.objects.get(code=problem_code)
        except Problem.DoesNotExist:
            return JsonResponse({"success": False, "error": "Problem not found"})

        task = improve_markdown_task.delay(problem.id)
        return JsonResponse({"success": True, "task_id": task.id})

    elif request.method == "POST":
        problem_code = request.POST.get("problem_code")
        improved_markdown = request.POST.get("improved_markdown", "")

        if not problem_code or not improved_markdown:
            return JsonResponse({"success": False, "error": "Missing required fields"})

        try:
            problem = Problem.objects.get(code=problem_code)
        except Problem.DoesNotExist:
            return JsonResponse({"success": False, "error": "Problem not found"})

        problem.description = improved_markdown
        problem.save(update_fields=["description"])
        return JsonResponse({"success": True})

    return JsonResponse({"success": False, "error": "Invalid method"})


@require_POST
def request_public(request):
    """Author requests a problem to be made public.

    With auto-review enabled, this creates a PublicRequest (or transitions
    existing one to PENDING), creates a new ProblemReviewRun, supersedes any prior
    run, and enqueues review_problem. Guards: permission, in-flight,
    dirty-check, cooldown.
    """
    if not request.user.is_authenticated:
        return HttpResponseForbidden()

    try:
        problem_id = int(request.POST.get("id"))
        problem = Problem.objects.get(id=problem_id)
    except Exception:
        return JsonResponse({"success": False, "error": "Problem not found"})

    # Guard 1: Permission
    if not problem.can_request_public_by(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"})

    # If auto-review is disabled, fall back to legacy behavior.
    if not getattr(settings, "AUTO_REVIEW_ENABLED", True):
        return _legacy_request_public(request, problem)

    new_hash = compute_input_hash(problem)

    # The in-flight + dirty-check + cooldown guards plus the run creation all
    # happen inside a single atomic block, with a SELECT … FOR UPDATE on the
    # Problem row serializing concurrent POSTs for the same problem. Without
    # this, two near-simultaneous Request Public clicks could both pass the
    # in-flight check, both pass dirty-check, and both create a new run —
    # leaving two rows where `superseded_by IS NULL`, breaking the dashboard's
    # "latest run" assumption.
    with transaction.atomic():
        Problem.objects.select_for_update().filter(id=problem.id).first()

        # Guard 2: In-flight (inside the lock so a racing peer's run is visible)
        if ProblemReviewRun.objects.filter(
            problem=problem, status=ProblemReviewRun.RUNNING
        ).exists():
            return JsonResponse(
                {
                    "success": False,
                    "error": _("Review currently running, please wait."),
                }
            )

        latest_run = (
            ProblemReviewRun.objects.filter(problem=problem)
            .order_by("-started_at")
            .first()
        )

        if latest_run is not None:
            # Guard 3: Dirty-check — admins bypass so they can re-run a
            # review without having to edit the problem first (useful for
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
            # Guard 4: Cooldown — only enforced when the *same* non-admin user
            # is re-requesting. Cases that bypass:
            #   - current user is superuser (admin diagnostic re-runs)
            #   - previous run was triggered by a different user (e.g. admin
            #     re-ran in between; clock shouldn't restart against the author)
            # The rate-limit's purpose is preventing one author from hammering
            # the button, not penalizing them for someone else's action.
            same_user_recently = (
                not request.user.is_superuser
                and latest_run.triggered_by_id == request.profile.id
            )
            if same_user_recently:
                cooldown_seconds = getattr(
                    settings, "AUTO_REVIEW_REQUEST_COOLDOWN_SECONDS", 300
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

        # All guards passed.
        existing = PublicRequest.objects.filter(problem=problem).first()
        if existing:
            existing.status = PublicRequest.PENDING
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
            PublicRequest.objects.create(
                problem=problem,
                requested_by=request.profile,
            )

        # Run creation + supersede + dispatch moved to a shared helper so the
        # contest-review path can reuse it without creating a PublicRequest.
        new_run = trigger_problem_review_for(
            problem, request.profile, dispatch="celery"
        )

    return JsonResponse(
        {
            "success": True,
            "run_id": new_run.id,
            "redirect": reverse("problem_review_dashboard", args=[problem.code]),
        }
    )


@require_POST
def cancel_request_public(request):
    """Author withdraws their PENDING PublicRequest so it disappears from
    the admin queue (e.g., misclicked, or wants to keep the problem private
    because they'll use it in a contest instead).

    Deletes the row outright — the prior request never produced state worth
    preserving (admin hadn't acted). Any ProblemReviewRun rows the request
    triggered remain (they're the review audit trail, separate from the
    publish-request state). Author can click Request Public again later.
    """
    if not request.user.is_authenticated:
        return HttpResponseForbidden()
    try:
        problem_id = int(request.POST.get("id"))
        problem = Problem.objects.get(id=problem_id)
    except Exception:
        return JsonResponse({"success": False, "error": "Problem not found"})
    if not problem.can_request_public_by(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"})
    deleted, _ignored = PublicRequest.objects.filter(
        problem=problem, status=PublicRequest.PENDING
    ).delete()
    return JsonResponse({"success": True, "cancelled": deleted})


def _legacy_request_public(request, problem):
    """Fallback when AUTO_REVIEW_ENABLED=False — pre-auto-review flow."""
    existing = PublicRequest.objects.filter(problem=problem).first()
    if existing:
        if existing.status == PublicRequest.PENDING:
            return JsonResponse(
                {
                    "success": False,
                    "error": _("A pending request already exists."),
                }
            )
        existing.status = PublicRequest.PENDING
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
        PublicRequest.objects.create(
            problem=problem,
            requested_by=request.profile,
        )
    _notify_superusers_new_request(problem, request.profile)
    return JsonResponse({"success": True})


def _notify_superusers_new_request(problem, requester):
    """Notify superusers about a new public request."""
    superuser_profiles = Profile.objects.filter(user__is_superuser=True).exclude(
        id=requester.id
    )
    queue_url = reverse("internal_problem_queue") + "?tab=request_public&status=P"
    problem_url = reverse("problem_detail", args=[problem.code])
    review_text = _("Review")
    html_link = (
        '<a href="%(problem_url)s">%(name)s</a>'
        ' (<a href="%(queue_url)s">%(review)s</a>)'
    ) % {
        "problem_url": problem_url,
        "name": problem.name,
        "queue_url": queue_url,
        "review": review_text,
    }

    for profile in superuser_profiles:
        Notification.objects.create_notification(
            owner=profile,
            category=NotificationCategory.PUBLIC_REQUEST_NEW,
            html_link=html_link,
            author=requester,
        )


def _notify_request_author(problem, reviewer, status):
    """Notify the request author about approval/rejection."""
    try:
        pr = problem.public_request
    except PublicRequest.DoesNotExist:
        return

    if status == PublicRequest.APPROVED:
        category = NotificationCategory.PUBLIC_REQUEST_APPROVED
    else:
        category = NotificationCategory.PUBLIC_REQUEST_REJECTED

    edit_url = reverse("problem_edit", args=[problem.code])
    html_link = '<a href="%(url)s">%(name)s</a>' % {
        "url": edit_url,
        "name": problem.name,
    }

    Notification.objects.create_notification(
        owner=pr.requested_by,
        category=category,
        html_link=html_link,
        author=reviewer,
    )


def problem_tag(request):
    """Handle AI tagging requests from the problem queue"""
    if not request.user.is_superuser:
        return HttpResponseForbidden()

    try:
        # Handle GET request - dispatch async Celery task for AI tagging
        if request.method == "GET":
            problem_code = request.GET.get("problem_code")
            if not problem_code:
                return JsonResponse(
                    {"success": False, "error": "Problem code is required"}
                )

            try:
                problem = Problem.objects.get(code=problem_code)
            except Problem.DoesNotExist:
                return JsonResponse({"success": False, "error": "Problem not found"})

            # Get current problem types and all types for the modal
            current_types = list(problem.types.values("id", "name"))
            all_types = list(ProblemType.objects.all().values("id", "name"))

            # Dispatch async Celery task
            task = tag_problem_task.delay(problem.id)

            return JsonResponse(
                {
                    "success": True,
                    "task_id": task.id,
                    "status": "processing",
                    "problem_code": problem.code,
                    "problem_name": problem.name,
                    "current_points": problem.points,
                    "current_types": current_types,
                    "all_types": all_types,
                }
            )

        # Handle POST request - apply the changes
        elif request.method == "POST":
            problem_code = request.POST.get("problem_code")
            if not problem_code:
                return JsonResponse(
                    {"success": False, "error": "Problem code is required"}
                )

            try:
                problem = Problem.objects.get(code=problem_code)
            except Problem.DoesNotExist:
                return JsonResponse({"success": False, "error": "Problem not found"})

            updated_info = []
            points_updated = False

            # Update points if provided
            points = request.POST.get("points")
            if points:
                try:
                    new_points = int(points)
                    old_points = problem.points
                    problem.points = new_points
                    points_updated = old_points != new_points
                    updated_info.append(f"Points: {new_points}")
                except ValueError:
                    return JsonResponse(
                        {"success": False, "error": "Invalid points value"}
                    )

            # Update types - handle both provided types and clearing all types
            type_ids = request.POST.getlist("types")
            try:
                if type_ids:
                    type_ids = [
                        int(tid) for tid in type_ids if tid
                    ]  # Filter out empty strings
                    type_objects = ProblemType.objects.filter(id__in=type_ids)
                    problem.types.set(type_objects)
                    type_names = ", ".join([t.name for t in type_objects])
                    updated_info.append(
                        f"Types: {type_names}" if type_names else "Types: (cleared)"
                    )
                else:
                    # If no types provided, clear all types
                    problem.types.clear()
                    updated_info.append("Types: (cleared)")
            except ValueError:
                return JsonResponse({"success": False, "error": "Invalid type IDs"})

            # Save the problem, bypassing points cap for non-public problems
            problem._bypass_points_cap = True
            problem.save()

            # Trigger rescoring if points changed
            if points_updated:
                transaction.on_commit(lambda: rescore_problem.delay(problem.id))

            return JsonResponse(
                {
                    "success": True,
                    "updated_info": " | ".join(updated_info) if updated_info else None,
                    "problem_code": problem.code,
                }
            )

    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Error in problem AI tagging: {e}")
        return JsonResponse({"success": False, "error": "An unexpected error occurred"})


class RequestTimeMixin(object):
    log_sort_fields = ()
    default_window = "24h"
    summary_limit = 50000

    def get_slow_threshold_ms(self):
        return getattr(settings, "SLOW_REQUEST_THRESHOLD_SECONDS", 5) * 1000

    def get_order(self, default):
        order = self.request.GET.get("order", default)
        if order not in self.log_sort_fields:
            return default
        return order

    def get_window_options(self):
        return (
            ("1h", _("1 hour")),
            ("6h", _("6 hours")),
            ("24h", _("24 hours")),
            ("7d", _("7 days")),
            ("all", _("Retained")),
        )

    def get_window_delta(self, window):
        return {
            "1h": timedelta(hours=1),
            "6h": timedelta(hours=6),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7),
        }.get(window)

    def get_retention_cutoff(self):
        retention_days = getattr(settings, "REQUEST_METRICS_RETENTION_DAYS", 7)
        if retention_days <= 0:
            return None
        return timezone.now() - timedelta(days=retention_days)

    def get_window(self):
        window = self.request.GET.get("window", self.default_window)
        valid_windows = {value for value, label in self.get_window_options()}
        if window not in valid_windows:
            return self.default_window
        return window

    def get_summary_limit(self):
        return getattr(settings, "REQUEST_METRICS_SUMMARY_LIMIT", self.summary_limit)

    def get_metric_queryset(self):
        if hasattr(self, "_metric_queryset"):
            return self._metric_queryset

        queryset = RequestMetric.objects.all()
        retention_cutoff = self.get_retention_cutoff()
        if retention_cutoff is not None:
            queryset = queryset.filter(time__gte=retention_cutoff)
        window_delta = self.get_window_delta(self.get_window())
        if window_delta is not None:
            queryset = queryset.filter(time__gte=timezone.now() - window_delta)

        route_query = self.request.GET.get("route", "").strip()
        if route_query:
            queryset = queryset.filter(
                Q(url_name__icontains=route_query) | Q(path__icontains=route_query)
            )

        username_query = self.request.GET.get("username", "").strip()
        if username_query:
            queryset = queryset.filter(username__icontains=username_query)

        auth_filter = self.request.GET.get("auth", "").strip()
        if auth_filter == "logged_in":
            queryset = queryset.filter(is_authenticated=True)
        elif auth_filter == "logged_out":
            queryset = queryset.filter(is_authenticated=False)

        method = self.request.GET.get("method", "").strip().upper()
        if method:
            queryset = queryset.filter(method=method)

        status = self.request.GET.get("status", "").strip()
        if status:
            try:
                queryset = queryset.filter(status_code=int(status))
            except ValueError:
                pass

        min_time = safe_float_or_none(self.request.GET.get("min_time"))
        if min_time is not None:
            queryset = queryset.filter(response_time_ms__gte=min_time)

        self._metric_queryset = self.apply_page_filter(queryset)
        return self._metric_queryset

    def apply_page_filter(self, queryset):
        return queryset

    def get_filter_context(self):
        return {
            "window": self.get_window(),
            "window_options": self.get_window_options(),
            "route_query": self.request.GET.get("route", "").strip(),
            "username_query": self.request.GET.get("username", "").strip(),
            "auth_filter": self.request.GET.get("auth", "").strip(),
            "method_filter": self.request.GET.get("method", "").strip().upper(),
            "status_filter": self.request.GET.get("status", "").strip(),
            "min_time_filter": self.request.GET.get("min_time", "").strip(),
        }

    def query_with(self, **overrides):
        params = self.request.GET.copy()
        for key, value in overrides.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        query_string = params.urlencode()
        return "?%s" % query_string if query_string else ""

    def get_url_name_param(self, url_name):
        return "None" if url_name is None else url_name

    def percentile(self, values, percentile):
        if not values:
            return None
        sorted_values = sorted(values)
        index = math.ceil(len(sorted_values) * percentile) - 1
        return sorted_values[max(index, 0)]

    def build_overview(self, metrics, route_count):
        response_times = [metric["response_time_ms"] for metric in metrics]
        db_times = [
            metric["db_time_ms"]
            for metric in metrics
            if metric["db_time_ms"] is not None
        ]
        cache_times = [
            metric["cache_time_ms"]
            for metric in metrics
            if metric["cache_time_ms"] is not None
        ]
        slow_count = sum(
            1
            for metric in metrics
            if metric["response_time_ms"] >= self.get_slow_threshold_ms()
        )
        profiled_count = sum(1 for metric in metrics if metric["profiler"])
        total_count = len(metrics)
        return {
            "sample_count": total_count,
            "route_count": route_count,
            "avg_time": sum(response_times) / total_count if total_count else None,
            "p95_time": self.percentile(response_times, 0.95),
            "max_time": max(response_times) if response_times else None,
            "slow_count": slow_count,
            "slow_rate": (slow_count / total_count * 100) if total_count else None,
            "avg_db_time": sum(db_times) / len(db_times) if db_times else None,
            "avg_cache_time": (
                sum(cache_times) / len(cache_times) if cache_times else None
            ),
            "profiled_count": profiled_count,
            "summary_limit": self.get_summary_limit(),
        }

    def build_route_summary(self, metrics):
        overview = self.build_overview(metrics, route_count=1 if metrics else 0)
        query_counts = [
            metric["db_query_count"]
            for metric in metrics
            if metric["db_query_count"] is not None
        ]
        cache_call_counts = [
            metric["cache_call_count"]
            for metric in metrics
            if metric["cache_call_count"] is not None
        ]
        response_times = [metric["response_time_ms"] for metric in metrics]
        db_times = [
            metric["db_time_ms"]
            for metric in metrics
            if metric["db_time_ms"] is not None
        ]
        cache_times = [
            metric["cache_time_ms"]
            for metric in metrics
            if metric["cache_time_ms"] is not None
        ]
        status_counts = {}
        method_counts = {}
        authenticated_count = 0
        for metric in metrics:
            status_counts[metric["status_code"]] = (
                status_counts.get(metric["status_code"], 0) + 1
            )
            method_counts[metric["method"]] = method_counts.get(metric["method"], 0) + 1
            if metric["is_authenticated"]:
                authenticated_count += 1

        overview.update(
            {
                "min_time": min(response_times) if response_times else None,
                "max_db_time": max(db_times) if db_times else None,
                "max_cache_time": max(cache_times) if cache_times else None,
                "avg_query_count": (
                    sum(query_counts) / len(query_counts) if query_counts else None
                ),
                "avg_cache_call_count": (
                    sum(cache_call_counts) / len(cache_call_counts)
                    if cache_call_counts
                    else None
                ),
                "db_ratio": (
                    overview["avg_db_time"] / overview["avg_time"] * 100
                    if overview["avg_db_time"] is not None and overview["avg_time"]
                    else None
                ),
                "cache_ratio": (
                    overview["avg_cache_time"] / overview["avg_time"] * 100
                    if overview["avg_cache_time"] is not None and overview["avg_time"]
                    else None
                ),
                "status_counts": sorted(status_counts.items()),
                "method_counts": sorted(method_counts.items()),
                "authenticated_count": authenticated_count,
                "anonymous_count": len(metrics) - authenticated_count,
            }
        )
        return overview


class InternalRequestTime(InternalView, ListView, RequestTimeMixin):
    title = _("Request times")
    template_name = "internal/request_time.html"
    context_object_name = "pages"
    list_url_name = "internal_request_time"
    detail_url_name = "internal_request_time_detail"
    page_type = "request_time"
    log_sort_fields = (
        "impact_ms",
        "avg_time",
        "p95_time",
        "max_time",
        "slow_count",
        "slow_rate",
        "count",
        "avg_db_time",
        "avg_query_count",
        "db_ratio",
        "avg_cache_time",
        "avg_cache_call_count",
        "cache_ratio",
    )

    def get_queryset(self):
        metrics = list(
            self.get_metric_queryset()
            .order_by("-time")
            .values(
                "url_name",
                "path",
                "response_time_ms",
                "db_query_count",
                "db_time_ms",
                "cache_call_count",
                "cache_time_ms",
                "time",
                "profiler",
            )[: self.get_summary_limit()]
        )
        table = {}
        for metric in metrics:
            url_name = metric["url_name"]
            if url_name not in table:
                table[url_name] = {
                    "total_time": 0,
                    "times": [],
                    "db_times": [],
                    "query_counts": [],
                    "cache_times": [],
                    "cache_call_counts": [],
                    "count": 0,
                    "slow_count": 0,
                    "max_time": 0,
                    "profiled_count": 0,
                    "latest": None,
                    "sample_path": metric["path"],
                    "url_name": url_name,
                    "url_name_display": url_name or _("Unresolved"),
                    "detail_query": self.query_with(
                        url_name=self.get_url_name_param(url_name), order=None
                    ),
                    "latest_query": self.query_with(
                        url_name=self.get_url_name_param(url_name), order="time"
                    ),
                    "db_query": self.query_with(
                        url_name=self.get_url_name_param(url_name), order="db_time_ms"
                    ),
                    "cache_query": self.query_with(
                        url_name=self.get_url_name_param(url_name),
                        order="cache_time_ms",
                    ),
                }
            response_time = metric["response_time_ms"]
            table[url_name]["count"] += 1
            table[url_name]["total_time"] += response_time
            table[url_name]["times"].append(response_time)
            table[url_name]["max_time"] = max(
                table[url_name]["max_time"], response_time
            )
            if response_time >= self.get_slow_threshold_ms():
                table[url_name]["slow_count"] += 1
            if metric["db_time_ms"] is not None:
                table[url_name]["db_times"].append(metric["db_time_ms"])
            if metric["db_query_count"] is not None:
                table[url_name]["query_counts"].append(metric["db_query_count"])
            if metric["cache_time_ms"] is not None:
                table[url_name]["cache_times"].append(metric["cache_time_ms"])
            if metric["cache_call_count"] is not None:
                table[url_name]["cache_call_counts"].append(metric["cache_call_count"])
            if metric["profiler"]:
                table[url_name]["profiled_count"] += 1
            if (
                table[url_name]["latest"] is None
                or metric["time"] > table[url_name]["latest"]
            ):
                table[url_name]["latest"] = metric["time"]

        pages = []
        for page in table.values():
            times = page.pop("times")
            db_times = page.pop("db_times")
            query_counts = page.pop("query_counts")
            cache_times = page.pop("cache_times")
            cache_call_counts = page.pop("cache_call_counts")
            page["avg_time"] = page["total_time"] / page["count"]
            page["p95_time"] = self.percentile(times, 0.95)
            page["impact_ms"] = page["total_time"]
            page["slow_rate"] = page["slow_count"] / page["count"] * 100
            page["avg_db_time"] = sum(db_times) / len(db_times) if db_times else None
            page["avg_query_count"] = (
                sum(query_counts) / len(query_counts) if query_counts else None
            )
            page["avg_cache_time"] = (
                sum(cache_times) / len(cache_times) if cache_times else None
            )
            page["avg_cache_call_count"] = (
                sum(cache_call_counts) / len(cache_call_counts)
                if cache_call_counts
                else None
            )
            page["db_ratio"] = (
                page["avg_db_time"] / page["avg_time"] * 100
                if page["avg_db_time"] is not None and page["avg_time"]
                else None
            )
            page["cache_ratio"] = (
                page["avg_cache_time"] / page["avg_time"] * 100
                if page["avg_cache_time"] is not None and page["avg_time"]
                else None
            )
            pages.append(page)

        self.overview = self.build_overview(metrics, len(pages))
        order = self.get_order("impact_ms")
        return sorted(
            pages,
            key=lambda x: x[order] if x[order] is not None else -1,
            reverse=True,
        )

    def get_context_data(self, **kwargs):
        context = super(InternalRequestTime, self).get_context_data(**kwargs)
        context["page_type"] = self.page_type
        context["title"] = self.title
        context["current_path"] = self.request.path
        context["detail_path"] = reverse(self.detail_url_name)
        context["slow_threshold_ms"] = self.get_slow_threshold_ms()
        context["overview"] = getattr(
            self, "overview", self.build_overview([], route_count=0)
        )
        context["filters"] = self.get_filter_context()
        context["order_query"] = lambda order: self.query_with(order=order)
        context["clear_filters_url"] = self.request.path
        return context


class InternalRequestTimeDetail(InternalRequestTime):
    template_name = "internal/request_time_detail.html"
    context_object_name = "requests"
    log_sort_fields = (
        "time",
        "response_time_ms",
        "status_code",
        "db_time_ms",
        "db_query_count",
        "cache_time_ms",
        "cache_call_count",
    )

    def get_profile_url(self, metric_id):
        query_string = urlencode({"return": self.request.get_full_path()})
        return "%s?%s" % (
            reverse("internal_request_metric_profile", kwargs={"metric_id": metric_id}),
            query_string,
        )

    def get_queryset(self):
        url_name = self.request.GET.get("url_name", None)
        if not url_name:
            return HttpResponseForbidden()
        if url_name == "None":
            url_name = None
        self.title = url_name
        queryset = self.get_metric_queryset().filter(url_name=url_name)
        order = self.get_order("response_time_ms")
        return queryset.order_by("-%s" % order)[:200]

    def get_context_data(self, **kwargs):
        context = super(InternalRequestTimeDetail, self).get_context_data(**kwargs)
        url_name = self.request.GET.get("url_name", None)
        if url_name == "None":
            url_name = None
        route_metrics = list(
            self.get_metric_queryset()
            .filter(url_name=url_name)
            .order_by("-time")
            .values(
                "response_time_ms",
                "db_query_count",
                "db_time_ms",
                "cache_call_count",
                "cache_time_ms",
                "status_code",
                "method",
                "is_authenticated",
                "profiler",
            )[: self.get_summary_limit()]
        )
        context["url_name"] = self.request.GET.get("url_name", None)
        context["url_name_display"] = self.title or _("Unresolved")
        context["order_query"] = lambda order: self.query_with(order=order)
        context["back_path"] = reverse(self.list_url_name)
        context["back_query"] = self.query_with(url_name=None, order=None)
        context["route_summary"] = self.build_route_summary(route_metrics)
        context["profile_url"] = self.get_profile_url
        return context


class InternalRequestMetricProfile(InternalView, TemplateView):
    title = _("Request profile")
    template_name = "internal/request_metric_profile.html"

    def get_context_data(self, **kwargs):
        context = super(InternalRequestMetricProfile, self).get_context_data(**kwargs)
        metric = get_object_or_404(RequestMetric, id=kwargs["metric_id"])
        back_url = self.request.GET.get("return") or reverse("internal_request_time")
        if not (
            back_url.startswith("/internal/request_time")
            or back_url.startswith("/internal/internal_slow_request")
        ):
            back_url = reverse("internal_request_time")
        cache_profile = metric.profiler.get("cache", {}) if metric.profiler else {}
        context["title"] = self.title
        context["page_type"] = "request_time"
        context["metric"] = metric
        context["back_url"] = back_url
        context["cache_profile"] = cache_profile
        context["slowest_queries"] = metric.profiler.get("slowest_queries", [])
        context["cache_operations"] = sorted(
            cache_profile.get("by_operation", {}).items()
        )
        return context


class InternalSlowRequest(InternalRequestTime):
    title = _("Slow requests")
    list_url_name = "internal_slow_request"
    detail_url_name = "internal_slow_request_detail"
    page_type = "slow_request"

    def apply_page_filter(self, queryset):
        return queryset.filter(response_time_ms__gte=self.get_slow_threshold_ms())


class InternalSlowRequestDetail(InternalRequestTimeDetail):
    title = _("Slow requests")
    list_url_name = "internal_slow_request"

    def apply_page_filter(self, queryset):
        return queryset.filter(response_time_ms__gte=self.get_slow_threshold_ms())


class InternalChatModeration(InternalView, ListView):
    model = ChatModerationLog
    title = _("Chat Moderation")
    template_name = "internal/chat_moderation.html"
    paginate_by = 50
    context_object_name = "logs"

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        return DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs,
        )

    def get_queryset(self):
        queryset = ChatModerationLog.objects.exclude(action="keep").select_related(
            "message__author__user", "moderator__user"
        )

        action_filter = self.request.GET.get("action", "")
        if action_filter == "mute":
            queryset = queryset.filter(action__in=["mute", "mute_temp", "mute_perm"])
        elif action_filter:
            queryset = queryset.filter(action=action_filter)

        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(message__author__user__username__icontains=search)
                | Q(message__body__icontains=search)
            )

        return queryset.order_by("-created_at")

    def _resolve_log(
        self,
        log,
        action,
        reason,
        moderator,
        mute_until=None,
        mute_duration_days=None,
    ):
        log.action = action
        log.reason = reason
        log.is_automated = False
        log.moderator = moderator
        log.mute_until = mute_until
        log.mute_duration_days = mute_duration_days
        log.save(
            update_fields=[
                "action",
                "reason",
                "is_automated",
                "moderator",
                "mute_until",
                "mute_duration_days",
            ]
        )

    def post(self, request, *args, **kwargs):
        log = get_object_or_404(
            ChatModerationLog.objects.select_related("message", "message__author"),
            id=request.POST.get("log"),
        )
        action = request.POST.get("action")
        reason = (request.POST.get("reason") or log.reason or "").strip()
        moderator = request.profile

        if action == "keep":
            self._resolve_log(log, "keep", reason, moderator)
            messages.success(request, _("Chat moderation case kept."))
        elif action == "hide":
            hide_lobby_message(
                log.message,
                moderator=moderator,
                reason=reason,
                log_action=False,
            )
            self._resolve_log(log, "hide", reason, moderator)
            messages.success(request, _("Chat message hidden."))
        elif action == "mute_temp":
            mute_result = mute_chat_user(
                log.message,
                moderator=moderator,
                reason=reason,
                mute_type="temporary",
                log_action=False,
            )
            self._resolve_log(
                log,
                "mute_temp",
                reason,
                moderator,
                mute_until=mute_result["mute_until"],
                mute_duration_days=mute_result["mute_duration_days"],
            )
            messages.success(request, _("User temporarily muted."))
        elif action == "mute_perm":
            mute_result = mute_chat_user(
                log.message,
                moderator=moderator,
                reason=reason,
                mute_type="permanent",
                log_action=False,
            )
            self._resolve_log(
                log,
                "mute_perm",
                reason,
                moderator,
                mute_until=mute_result["mute_until"],
                mute_duration_days=mute_result["mute_duration_days"],
            )
            messages.success(request, _("User permanently muted."))
        else:
            messages.error(request, _("Unknown moderation action."))

        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER", reverse("internal_chat_moderation"))
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "chat_moderation"
        context["title"] = self.title
        context["action_filter"] = self.request.GET.get("action", "")
        context["search_query"] = self.request.GET.get("search", "")
        query_params = self.request.GET.copy()
        if "page" in query_params:
            del query_params["page"]
        if query_params:
            query_string = query_params.urlencode()
            context["page_prefix"] = self.request.path + "?" + query_string + "&page="
            context["first_page_href"] = self.request.path + "?" + query_string
        else:
            context["page_prefix"] = self.request.path + "?page="
            context["first_page_href"] = self.request.path

        return context


class InternalCommentModeration(InternalView, ListView):
    model = CommentModerationLog
    title = _("Comment Moderation")
    template_name = "internal/comment_moderation.html"
    paginate_by = 50
    context_object_name = "logs"

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        return DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs,
        )

    def get_queryset(self):
        queryset = CommentModerationLog.objects.exclude(
            action=CommentModerationLog.ACTION_KEEP
        ).select_related(
            "comment",
            "comment__author__user",
            "comment__content_type",
            "moderator__user",
        )

        action_filter = self.request.GET.get("action", "")
        if action_filter == "mute":
            queryset = queryset.filter(
                action__in=[
                    CommentModerationLog.ACTION_MUTE_TEMP,
                    CommentModerationLog.ACTION_MUTE_PERM,
                ]
            )
        elif action_filter:
            queryset = queryset.filter(action=action_filter)

        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(comment__author__user__username__icontains=search)
                | Q(comment__body__icontains=search)
            )

        return queryset.order_by("-created_at")

    def _resolve_log(
        self,
        log,
        action,
        reason,
        moderator,
        mute_until=None,
        mute_duration_days=None,
    ):
        log.action = action
        log.reason = reason
        log.is_automated = False
        log.moderator = moderator
        log.mute_until = mute_until
        log.mute_duration_days = mute_duration_days
        log.save(
            update_fields=[
                "action",
                "reason",
                "is_automated",
                "moderator",
                "mute_until",
                "mute_duration_days",
            ]
        )

    def post(self, request, *args, **kwargs):
        log = get_object_or_404(
            CommentModerationLog.objects.select_related("comment", "comment__author"),
            id=request.POST.get("log"),
        )
        action = request.POST.get("action")
        reason = (request.POST.get("reason") or log.reason or "").strip()
        moderator = request.profile

        if action == "keep":
            self._resolve_log(
                log,
                CommentModerationLog.ACTION_KEEP,
                reason,
                moderator,
            )
            messages.success(request, _("Comment moderation case kept."))
        elif action == "hide":
            hide_comment_for_moderation(
                log.comment,
                reason=reason,
                moderator=moderator,
                log_action=False,
            )
            self._resolve_log(
                log,
                CommentModerationLog.ACTION_HIDE,
                reason,
                moderator,
            )
            messages.success(request, _("Comment hidden."))
        elif action == "mute_temp":
            mute_result = mute_comment_author(
                log.comment,
                reason=reason,
                moderator=moderator,
                mute_type="temporary",
                log_action=False,
            )
            self._resolve_log(
                log,
                CommentModerationLog.ACTION_MUTE_TEMP,
                reason,
                moderator,
                mute_until=mute_result["mute_until"],
                mute_duration_days=mute_result["mute_duration_days"],
            )
            messages.success(request, _("User temporarily muted."))
        elif action == "mute_perm":
            mute_result = mute_comment_author(
                log.comment,
                reason=reason,
                moderator=moderator,
                mute_type="permanent",
                log_action=False,
            )
            self._resolve_log(
                log,
                CommentModerationLog.ACTION_MUTE_PERM,
                reason,
                moderator,
                mute_until=mute_result["mute_until"],
                mute_duration_days=mute_result["mute_duration_days"],
            )
            messages.success(request, _("User permanently muted."))
        else:
            messages.error(request, _("Unknown moderation action."))

        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER", reverse("internal_comment_moderation"))
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "comment_moderation"
        context["title"] = self.title
        context["action_filter"] = self.request.GET.get("action", "")
        context["search_query"] = self.request.GET.get("search", "")
        logs = list(context["logs"])
        context_details = get_comment_context_details([log.comment for log in logs])
        for log in logs:
            detail = context_details.get(log.comment_id, {})
            log.comment_context_title = detail.get("title", "")
            log.comment_context_url = detail.get("url", "")
        context["logs"] = logs
        query_params = self.request.GET.copy()
        if "page" in query_params:
            del query_params["page"]
        if query_params:
            query_string = query_params.urlencode()
            context["page_prefix"] = self.request.path + "?" + query_string + "&page="
            context["first_page_href"] = self.request.path + "?" + query_string
        else:
            context["page_prefix"] = self.request.path + "?page="
            context["first_page_href"] = self.request.path

        return context


class InternalUsernameModeration(InternalView, ListView):
    model = UsernameModerationCase
    title = _("Username Moderation")
    template_name = "internal/username_moderation.html"
    paginate_by = 50
    context_object_name = "cases"

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        return DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs,
        )

    def get_queryset(self):
        queryset = UsernameModerationCase.objects.select_related(
            "user", "user__profile", "moderator__user"
        )

        status_filter = self.request.GET.get("status", "")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        decision_filter = self.request.GET.get("decision", "")
        if decision_filter:
            queryset = queryset.filter(decision=decision_filter)
        else:
            queryset = queryset.exclude(decision=UsernameModerationCase.DECISION_ALLOW)

        category_filter = self.request.GET.get("category", "")
        if category_filter:
            queryset = queryset.filter(category=category_filter)

        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(username__icontains=search) | Q(user__username__icontains=search)
            )

        return queryset.order_by("-created_at")

    def post(self, request, *args, **kwargs):
        case = get_object_or_404(UsernameModerationCase, id=request.POST.get("case"))
        action = request.POST.get("action")
        moderator = request.profile

        if action == "allow":
            case.allow(moderator=moderator)
            messages.success(request, _("Username moderation case allowed."))
        elif action == "disable":
            case.disable_user(moderator=moderator, hide_identity=True)
            messages.success(request, _("User disabled and public identity hidden."))
        elif action == "hide":
            case.hide_public_identity(moderator=moderator)
            messages.success(request, _("Public identity hidden."))
        elif action == "unhide":
            case.unhide_public_identity(moderator=moderator)
            messages.success(request, _("Public identity restored."))
        else:
            messages.error(request, _("Unknown moderation action."))

        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER", reverse("internal_username_moderation"))
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "username_moderation"
        context["title"] = self.title
        context["status_filter"] = self.request.GET.get("status", "")
        context["decision_filter"] = self.request.GET.get("decision", "")
        context["category_filter"] = self.request.GET.get("category", "")
        context["search_query"] = self.request.GET.get("search", "")
        context["status_choices"] = UsernameModerationCase.STATUS_CHOICES
        context["decision_choices"] = UsernameModerationCase.DECISION_CHOICES
        context["category_choices"] = UsernameModerationCase.CATEGORY_CHOICES

        query_params = self.request.GET.copy()
        if "page" in query_params:
            del query_params["page"]
        if query_params:
            query_string = query_params.urlencode()
            context["page_prefix"] = self.request.path + "?" + query_string + "&page="
            context["first_page_href"] = self.request.path + "?" + query_string
        else:
            context["page_prefix"] = self.request.path + "?page="
            context["first_page_href"] = self.request.path
        return context


@require_POST
def unmute_user(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    try:
        profile_id = int(request.POST.get("id"))
        profile = Profile.objects.get(id=profile_id)
    except Exception:
        return JsonResponse({"success": False, "error": "User not found"})

    profile.mute = False
    profile.mute_until = None
    profile.mute_reason = ""
    profile.save(update_fields=["mute", "mute_until", "mute_reason"])
    Profile.dirty_cache(profile.id)
    event.post(
        encrypt_channel("chat_" + str(profile.id)),
        {
            "type": "chat_unmuted",
        },
    )
    return JsonResponse({"success": True})
