import logging
import json

from django.conf import settings
from django.views.generic import ListView
from django.utils.translation import gettext as _, get_language
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_POST

from judge.utils.strings import safe_float_or_none
from judge.models.problem import get_distinct_problem_points
from judge.models import Profile

from judge.utils.diggpaginator import DiggPaginator
from judge.models import Problem, ProblemType
from judge.models.public_request import PublicRequest
from judge.models.notification import Notification, NotificationCategory
from judge.tasks import rescore_problem
from judge.tasks.llm import tag_problem_task, improve_markdown_task
from chat_box.models import ChatModerationLog


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
            "message__author__user"
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
