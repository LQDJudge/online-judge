from django.contrib.auth.decorators import login_required
from django.views.generic import ListView
from django.utils.translation import ugettext as _
from django.utils.timezone import now
from django.db.models import BooleanField, Value

from judge.utils.cachedict import CacheDict
from judge.models import Profile, Comment, Notification

__all__ = ["NotificationList"]


class NotificationList(ListView):
    model = Notification
    context_object_name = "notifications"
    template_name = "notification/list.html"

    def get_queryset(self):
        self.unseen_cnt = self.request.profile.count_unseen_notifications

        query = {
            "owner": self.request.profile,
        }

        self.queryset = (
            Notification.objects.filter(**query)
            .order_by("-time")[:100]
            .annotate(seen=Value(True, output_field=BooleanField()))
        )

        # Mark the several first unseen
        for cnt, q in enumerate(self.queryset):
            if cnt < self.unseen_cnt:
                q.seen = False
            else:
                break

        return self.queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unseen_count"] = self.unseen_cnt
        context["title"] = _("Notifications (%d unseen)" % context["unseen_count"])
        context["has_notifications"] = self.queryset.exists()
        context["page_titles"] = CacheDict(lambda page: Comment.get_page_title(page))
        return context

    def get(self, request, *args, **kwargs):
        ret = super().get(request, *args, **kwargs)

        # update after rendering
        Notification.objects.filter(owner=self.request.profile).update(read=True)

        return ret
