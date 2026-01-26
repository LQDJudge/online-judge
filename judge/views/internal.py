import logging
import json

from django.conf import settings
from django.views.generic import ListView
from django.utils.translation import gettext as _, get_language
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse

from judge.utils.strings import safe_float_or_none
from judge.models.problem import get_distinct_problem_points
from judge.models import Profile

from judge.utils.diggpaginator import DiggPaginator
from judge.models import Problem, ProblemType
from judge.tasks import rescore_problem
from judge.tasks.llm import tag_problem_task


class InternalView(object):
    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return super(InternalView, self).get(request, *args, **kwargs)
        return HttpResponseForbidden()


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

        # Base queryset - public problems only
        queryset = Problem.objects.filter(is_public=True, is_organization_private=False)

        # Apply search filter (same logic as ProblemList)
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

        # Apply author filter
        if self.author_query:
            queryset = queryset.filter(authors__in=self.author_query)

        # Apply point range filter
        if self.point_start is not None:
            queryset = queryset.filter(points__gte=self.point_start)
        if self.point_end is not None:
            queryset = queryset.filter(points__lte=self.point_end)

        return queryset.distinct().order_by("-id")

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

            # Save the problem
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
