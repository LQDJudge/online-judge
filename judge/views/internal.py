import difflib
import json
import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import Http404, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _, get_language
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView, View

from chat_box.models import ChatModerationLog
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
    clamp_limit,
    search_problems,
    similar_problems,
)
from judge.models import Problem, ProblemType, Profile, Submission
from judge.models.notification import Notification, NotificationCategory
from judge.models.problem import get_distinct_problem_points
from judge.models.public_request import PublicRequest
from judge.tasks import rescore_problem
from judge.tasks.llm import improve_markdown_task, tag_problem_task
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.problem_equivalence import (
    ProblemEquivalenceError,
    ProblemEquivalenceVerifier,
)
from judge.utils.problem_merge import ProblemMerge
from judge.utils.strings import safe_float_or_none

logger = logging.getLogger(__name__)


class InternalView(object):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return super(InternalView, self).dispatch(request, *args, **kwargs)
        return HttpResponseForbidden()

    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return super(InternalView, self).get(request, *args, **kwargs)
        return HttpResponseForbidden()


class InternalSemanticSearch(InternalView, TemplateView):
    title = _("Semantic Search")
    template_name = "internal/semantic_search.html"

    def get(self, request, *args, **kwargs):
        if not getattr(settings, "USE_ML", False):
            raise Http404()
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "semantic_search"
        context["title"] = self.title
        context["default_limit"] = 20
        context["max_limit"] = 50
        return context


class InternalSemanticSearchApi(InternalView, View):
    def get(self, request, *args, **kwargs):
        if not getattr(settings, "USE_ML", False):
            raise Http404()

        query = request.GET.get("q", "").strip()
        if not query:
            return JsonResponse({"error": _("Query is required")}, status=400)

        limit = clamp_limit(request.GET.get("limit"))
        try:
            results = search_problems(query, limit=limit)
        except SemanticSearchUnavailable as exc:
            return JsonResponse({"error": str(exc)}, status=503)
        except Exception as exc:
            logger.error("Semantic search failed: %s", exc, exc_info=True)
            return JsonResponse({"error": str(exc)}, status=500)

        return JsonResponse({"query": query, "limit": limit, "results": results})


class InternalSimilarProblemsApi(InternalView, View):
    def get(self, request, *args, **kwargs):
        if not getattr(settings, "USE_ML", False):
            raise Http404()

        problem_id = request.GET.get("problem_id", "").strip()
        problem_code = request.GET.get("problem", "").strip()
        if not problem_id and not problem_code:
            return JsonResponse({"error": _("Problem is required")}, status=400)

        try:
            if problem_id:
                problem = Problem.objects.get(id=problem_id)
            else:
                problem = Problem.objects.get(code=problem_code)
        except (Problem.DoesNotExist, ValueError):
            return JsonResponse({"error": _("Problem not found")}, status=404)

        limit = clamp_limit(request.GET.get("limit"))
        try:
            results = similar_problems(problem, limit=limit)
        except SemanticSearchUnavailable as exc:
            return JsonResponse({"error": str(exc)}, status=503)
        except Exception as exc:
            logger.error("Similar problem search failed: %s", exc, exc_info=True)
            return JsonResponse({"error": str(exc)}, status=500)

        return JsonResponse(
            {"problem": problem.code, "limit": limit, "results": results}
        )


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
        """Request public queue: problems with PublicRequest records."""
        queryset = Problem.objects.filter(public_request__isnull=False).select_related(
            "public_request",
            "public_request__requested_by",
            "public_request__reviewed_by",
        )

        if self.status_filter:
            queryset = queryset.filter(public_request__status=self.status_filter)

        queryset = self._apply_search_filters(queryset)
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

        # Request counts for tab badges
        context["pending_request_count"] = PublicRequest.objects.filter(
            status=PublicRequest.PENDING
        ).count()

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

    # Notify the author
    _notify_request_author(problem, request.profile, PublicRequest.APPROVED)

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

    # Notify the author
    _notify_request_author(problem, request.profile, PublicRequest.REJECTED)

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
            Problem.objects.get(code=problem_code)
        except Problem.DoesNotExist:
            return JsonResponse({"success": False, "error": "Problem not found"})

        task = improve_markdown_task.delay(problem_code)
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
    """Author requests a problem to be made public."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden()

    try:
        problem_id = int(request.POST.get("id"))
        problem = Problem.objects.get(id=problem_id)
    except Exception:
        return JsonResponse({"success": False, "error": "Problem not found"})

    # Must be an editor of the problem
    if not problem.is_editable_by(request.user):
        return JsonResponse({"success": False, "error": "Permission denied"})

    # Check if there's already a pending request
    existing = PublicRequest.objects.filter(problem=problem).first()
    if existing:
        if existing.status == PublicRequest.PENDING:
            return JsonResponse(
                {"success": False, "error": _("A pending request already exists.")}
            )
        # Re-request after rejection: update existing record
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

    # Notify superusers
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
            task = tag_problem_task.delay(problem_code)

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
    def get_requests_data(self):
        logger = logging.getLogger(self.log_name)
        log_filename = logger.handlers[0].baseFilename
        requests = []

        with open(log_filename, "r") as f:
            for line in f:
                try:
                    info = json.loads(line)
                    requests.append(info)
                except:
                    continue
        return requests


class InternalRequestTime(InternalView, ListView, RequestTimeMixin):
    title = _("Request times")
    template_name = "internal/request_time.html"
    context_object_name = "pages"
    log_name = "judge.request_time"
    detail_url_name = "internal_request_time_detail"
    page_type = "request_time"

    def get_queryset(self):
        requests = self.get_requests_data()
        table = {}
        for r in requests:
            url_name = r["url_name"]
            if url_name not in table:
                table[url_name] = {
                    "time": 0,
                    "count": 0,
                    "url_name": url_name,
                }
            old_sum = table[url_name]["time"] * table[url_name]["count"]
            table[url_name]["count"] += 1
            table[url_name]["time"] = (old_sum + float(r["response_time"])) / table[
                url_name
            ]["count"]
        order = self.request.GET.get("order", "time")
        return sorted(table.values(), key=lambda x: x[order], reverse=True)

    def get_context_data(self, **kwargs):
        context = super(InternalRequestTime, self).get_context_data(**kwargs)
        context["page_type"] = self.page_type
        context["title"] = self.title
        context["current_path"] = self.request.path
        context["detail_path"] = reverse(self.detail_url_name)
        return context


class InternalRequestTimeDetail(InternalRequestTime):
    template_name = "internal/request_time_detail.html"
    context_object_name = "requests"

    def get_queryset(self):
        url_name = self.request.GET.get("url_name", None)
        if not url_name:
            return HttpResponseForbidden()
        if url_name == "None":
            url_name = None
        self.title = url_name
        requests = self.get_requests_data()
        filtered_requests = [r for r in requests if r["url_name"] == url_name]
        order = self.request.GET.get("order", "response_time")
        return sorted(filtered_requests, key=lambda x: x[order], reverse=True)[:200]

    def get_context_data(self, **kwargs):
        context = super(InternalRequestTimeDetail, self).get_context_data(**kwargs)
        context["url_name"] = self.request.GET.get("url_name", None)
        return context


class InternalSlowRequest(InternalRequestTime):
    log_name = "judge.slow_request"
    detail_url_name = "internal_slow_request_detail"
    page_type = "slow_request"


class InternalSlowRequestDetail(InternalRequestTimeDetail):
    log_name = "judge.slow_request"


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
        if action_filter:
            queryset = queryset.filter(action=action_filter)

        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                message__author__user__username__icontains=search
            )

        return queryset.order_by("-created_at")

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
    profile.save(update_fields=["mute"])
    Profile.dirty_cache(profile.id)
    return JsonResponse({"success": True})
