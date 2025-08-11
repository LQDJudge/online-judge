import logging
import json

from django.views.generic import ListView
from django.utils.translation import gettext as _, gettext_lazy
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.shortcuts import render

from judge.utils.diggpaginator import DiggPaginator
from judge.models import Problem, ProblemType
from judge.tasks import rescore_problem

from problem_tag import get_problem_tag_service


class InternalView(object):
    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return super(InternalView, self).get(request, *args, **kwargs)
        return HttpResponseForbidden()


class InternalProblem(InternalView, ListView):
    model = Problem
    title = _("Internal problems")
    template_name = "internal/problem/problem.html"
    paginate_by = 100
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

    def get_search_query(self):
        return self.request.GET.get("q") or self.request.POST.get("q")

    def get_queryset(self):
        queryset = Problem.objects.annotate(
            vote_count=Count("volunteer_user_votes")
        ).filter(vote_count__gte=1)
        query = self.get_search_query()
        if query:
            queryset = queryset.filter(
                Q(code__icontains=query) | Q(name__icontains=query)
            )
        return queryset.order_by("-vote_count")

    def get_context_data(self, **kwargs):
        context = super(InternalProblem, self).get_context_data(**kwargs)
        context["page_type"] = "problem"
        context["title"] = self.title
        context["page_prefix"] = self.request.path + "?page="
        context["first_page_href"] = self.request.path
        context["query"] = self.get_search_query()

        return context


def get_problem_votes(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden()
    try:
        problem = Problem.objects.get(id=request.GET.get("id"))
    except:
        return HttpResponseForbidden()
    votes = (
        problem.volunteer_user_votes.select_related("voter")
        .prefetch_related("types")
        .order_by("id")
    )
    return render(
        request,
        "internal/problem/votes.html",
        {
            "problem": problem,
            "votes": votes,
        },
    )


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

    def get_queryset(self):
        queryset = Problem.objects.filter(is_public=True, is_organization_private=False)
        return queryset.order_by("-id")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "problem_queue"
        context["title"] = self.title
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
        # Handle GET request - show suggestions modal
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

            # Use the problem tag service
            tag_service = get_problem_tag_service()

            # Call the AI service to analyze the problem
            result = tag_service.tag_single_problem(problem)

            if result["success"]:
                # Get current problem types for pre-selection
                current_types = list(problem.types.values("id", "name"))

                # Get all problem types for the select2 dropdown
                all_types = list(ProblemType.objects.all().values("id", "name"))

                # Convert predicted type names to type objects
                predicted_types = []
                if result.get("predicted_types"):
                    predicted_types = list(
                        ProblemType.objects.filter(
                            name__in=result["predicted_types"]
                        ).values("id", "name")
                    )

                return JsonResponse(
                    {
                        "success": True,
                        "is_valid": result["is_valid"],
                        "problem_code": problem.code,
                        "problem_name": problem.name,
                        "current_points": problem.points,
                        "predicted_points": result.get("predicted_points"),
                        "current_types": current_types,
                        "predicted_types": predicted_types,
                        "all_types": all_types,
                    }
                )
            else:
                return JsonResponse(
                    {
                        "success": False,
                        "error": result.get("error", "Unknown error occurred"),
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
