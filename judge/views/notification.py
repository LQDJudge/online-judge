from django.contrib.auth.decorators import login_required
from django.views.generic import ListView, View
from django.utils.translation import gettext as _
from django.utils.timezone import now
from django.http import Http404, JsonResponse, HttpResponseRedirect
from django.urls import reverse
from django.contrib import messages
from django.db.models import Q

from judge.models import Notification, NotificationProfile, Profile
from judge.models.notification import unseen_notifications_count, NotificationCategory
from judge.utils.infinite_paginator import InfinitePaginationMixin

__all__ = ["NotificationList", "NotificationMarkAsRead"]


class NotificationList(InfinitePaginationMixin, ListView):
    model = Notification
    context_object_name = "notifications"
    template_name = "notification/list.html"
    paginate_by = 50

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            raise Http404()

        # Get filter parameters
        category = self.request.GET.get("category", "")
        status = self.request.GET.get("status", "")  # 'read', 'unread', or ''
        author = self.request.GET.get("author", "")
        search = self.request.GET.get("search", "")

        # Use the enhanced filtering method
        queryset = Notification.objects.get_filtered_notifications(
            owner=self.request.profile,
            category=category if category else None,
            is_read=status == "read" if status else None,
            author=(
                Profile.objects.filter(user__username=author).first()
                if author
                else None
            ),
            search=search if search else None,
        )

        self.unseen_cnt = unseen_notifications_count(self.request.profile)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unseen_count"] = self.unseen_cnt
        context["title"] = _("Notifications (%d unseen)") % context["unseen_count"]
        context["first_page_href"] = "."

        # Add filter context
        context["current_category"] = self.request.GET.get("category", "")
        context["current_status"] = self.request.GET.get("status", "")
        context["current_author"] = self.request.GET.get("author", "")
        context["current_search"] = self.request.GET.get("search", "")

        # Add available categories for filter dropdown
        context["notification_categories"] = NotificationCategory.choices

        # Add statistics
        total_notifications = Notification.objects.filter(
            owner=self.request.profile
        ).count()
        unread_notifications = Notification.objects.filter(
            owner=self.request.profile, is_read=False
        ).count()
        context["total_notifications"] = total_notifications
        context["unread_notifications"] = unread_notifications

        return context

    def get(self, request, *args, **kwargs):
        ret = super().get(request, *args, **kwargs)
        if not request.user.is_authenticated:
            raise Http404()

        return ret


class NotificationMarkAsRead(View):
    """AJAX view to mark specific notifications as read"""

    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Authentication required"}, status=401)

        notification_ids = request.POST.getlist("notification_ids[]")
        if not notification_ids:
            return JsonResponse({"error": "No notification IDs provided"}, status=400)

        try:
            count = Notification.objects.mark_as_read(
                user=request.profile, notification_ids=notification_ids
            )
            return JsonResponse(
                {
                    "success": True,
                    "marked_count": count,
                    "new_unread_count": unseen_notifications_count(request.profile),
                }
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)
