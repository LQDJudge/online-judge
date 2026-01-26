from django.utils.translation import gettext as _

from judge.models import Comment
from judge.views.feed import HomeFeedView


class CommentFeed(HomeFeedView):
    model = Comment
    context_object_name = "comments"
    paginate_by = 50
    feed_content_template_name = "comments/feed.html"

    def get_queryset(self):
        view_type = self.request.GET.get("view", "all")
        content_filter = self.request.GET.get("content", "all")

        # Overfetch before filtering
        needed_count = min(500, self.page * self.paginate_by * 2)

        return Comment.most_recent(
            user=self.request.user,
            view_type=view_type,
            content_filter=content_filter,
            organization=self.request.organization,
            n=needed_count,
        )

    def get_context_data(self, **kwargs):
        context = super(CommentFeed, self).get_context_data(**kwargs)
        context["title"] = _("Comment feed")
        context["page_type"] = "comment"
        context["view_type"] = self.request.GET.get("view", "all")
        context["content_filter"] = self.request.GET.get("content", "all")

        return context
