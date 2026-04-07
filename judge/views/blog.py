from django.http import Http404
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from django.views.generic import View

from judge.views.comment import CommentableMixin
from judge.views.pagevote import PageVoteDetailView
from judge.views.bookmark import BookMarkDetailView
from judge.models import BlogPost
from judge.utils.views import TitleMixin
from judge.utils.feed import build_home_feed
from judge.views.feed import HomeFeedView


class PostList(HomeFeedView):
    model = BlogPost
    paginate_by = 4
    context_object_name = "posts"
    feed_content_template_name = "home/feed-content.html"
    url_name = "blog_post_list"

    def get(self, request, *args, **kwargs):
        # For logged-in users: cursor-based mixed feed
        if request.user.is_authenticated:
            only_content = request.GET.get("only_content")
            cursor_str = request.GET.get("cursor")

            feed_result = build_home_feed(request, cursor_str=cursor_str)

            if only_content and self.feed_content_template_name:
                # AJAX infinite scroll
                context = {
                    "feed_items": feed_result["items"],
                    "has_next_page": feed_result["has_next_page"],
                    "next_cursor": feed_result["next_cursor"],
                }
                return render(request, self.feed_content_template_name, context)

            # Full page load — need sidebar context from HomeFeedView
            self.feed_result = feed_result
            self.page = 1
            return super(HomeFeedView, self).get(request, *args, **kwargs)

        # For logged-out users: simple post pagination (old behavior)
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        """Queryset for logged-out users (simple post pagination)."""
        queryset = BlogPost.objects.filter(
            visible=True,
            publish_on__lte=timezone.now(),
            is_organization_private=False,
        )
        return queryset.order_by("-sticky", "-publish_on")

    def get_context_data(self, **kwargs):
        context = super(PostList, self).get_context_data(**kwargs)
        context["title"] = self.title or _("Home")
        context["page_type"] = "blog"
        context["show_organization_private_icon"] = True

        # For logged-in users: use pre-built feed result
        if hasattr(self, "feed_result") and self.feed_result:
            context["feed_items"] = self.feed_result["items"]
            context["has_next_page"] = self.feed_result["has_next_page"]
            context["next_cursor"] = self.feed_result["next_cursor"]
        else:
            # Logged-out: prefetch for blog/content.html
            BlogPost.prefetch_organization_ids(*[post.id for post in context["posts"]])

        return context

    def get_feed_context(self, object_list):
        """For logged-out AJAX pagination only."""
        context = {}
        context["show_organization_private_icon"] = True
        BlogPost.prefetch_organization_ids(*[post.id for post in object_list])
        return context


class PostView(
    TitleMixin,
    CommentableMixin,
    PageVoteDetailView,
    BookMarkDetailView,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    model = BlogPost
    pk_url_kwarg = "id"
    context_object_name = "post"
    template_name = "blog/blog.html"

    def get_title(self):
        return self.object.title

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(
            self.get_context_data(
                object=self.object,
            )
        )

    def get_context_data(self, **kwargs):
        context = super(PostView, self).get_context_data(**kwargs)
        context["og_image"] = self.object.og_image
        context["editable_orgs"] = []

        context["organizations"] = self.object.get_organizations()

        if self.request.profile:
            is_author = self.request.profile.id in self.object.get_author_ids()
            for org in context["organizations"]:
                if self.request.profile.can_edit_organization(org):
                    context["editable_orgs"].append(org)
                elif org.can_moderate(self.request.profile):
                    context["editable_orgs"].append(org)
                elif is_author and not self.object.visible:
                    context["editable_orgs"].append(org)

        if self.object.is_editable_by(self.request.user):
            context["num_open_tickets"] = self.object.tickets.filter(
                is_open=True
            ).count()

        context = self.get_comment_context(context)

        return context

    def get_object(self, queryset=None):
        post = super(PostView, self).get_object(queryset)
        if not post.is_accessible_by(self.request.user):
            raise Http404()
        return post
