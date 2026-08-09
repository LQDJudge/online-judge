from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils.translation import gettext as _
from django.views.generic import ListView, View

from judge.models import Notification, Profile
from judge.models.notification import NotificationCategory, unseen_notifications_count
from judge.utils.infinite_paginator import InfinitePaginationMixin

__all__ = ["NotificationList", "NotificationMarkAsRead"]


class NotificationList(LoginRequiredMixin, InfinitePaginationMixin, ListView):
    model = Notification
    context_object_name = "notifications"
    template_name = "notification/list.html"
    paginate_by = 50

    def get_queryset(self):
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

        # Compute all category and page-level statistics in one grouped query.
        # Categories with no notifications are filled with zero below so the
        # filter remains complete. Python's stable sort preserves the existing
        # category order when unseen counts are tied.
        category_stats = {
            row["category"]: row
            for row in Notification.objects.filter(owner=self.request.profile)
            .values("category")
            .annotate(
                total=Count("id"),
                unseen=Count("id", filter=Q(is_read=False)),
            )
        }
        notification_categories = [
            (value, label, category_stats.get(value, {}).get("unseen", 0))
            for value, label in NotificationCategory.choices
        ]
        notification_categories.sort(key=lambda item: -item[2])

        context["notification_categories"] = notification_categories
        context["total_notifications"] = sum(
            row["total"] for row in category_stats.values()
        )
        context["unread_notifications"] = sum(
            row["unseen"] for row in category_stats.values()
        )

        return context


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
