from django.contrib.auth.decorators import login_required
from django.views.generic import ListView
from django.utils.translation import ugettext as _
from django.utils.timezone import now
from django.http import Http404

from judge.models import Notification, NotificationProfile
from judge.models.notification import unseen_notifications_count
from judge.utils.infinite_paginator import InfinitePaginationMixin

__all__ = ["NotificationList"]


class NotificationList(InfinitePaginationMixin, ListView):
    model = Notification
    context_object_name = "notifications"
    template_name = "notification/list.html"
    paginate_by = 50

    def get_queryset(self):
        self.unseen_cnt = unseen_notifications_count(self.request.profile)

        self.queryset = Notification.objects.filter(
            owner=self.request.profile
        ).order_by("-id")

        return self.queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unseen_count"] = self.unseen_cnt
        context["title"] = _("Notifications (%d unseen)") % context["unseen_count"]
        context["first_page_href"] = "."
        return context

    def get(self, request, *args, **kwargs):
        ret = super().get(request, *args, **kwargs)
        if not request.user.is_authenticated:
            raise Http404()
        NotificationProfile.objects.filter(user=request.profile).update(unread_count=0)
        unseen_notifications_count.dirty(self.request.profile)
        return ret
