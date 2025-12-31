from django.db.models import Count, Max, Q, Case, When, Prefetch
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import lazy
from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from django.views.generic import View

from judge.views.comment import CommentableMixin
from judge.views.pagevote import PageVoteDetailView
from judge.views.bookmark import BookMarkDetailView
from judge.models import (
    BlogPost,
    Comment,
)
from judge.utils.views import TitleMixin
from judge.views.feed import FeedView, HomeFeedView
from judge.models.comment import get_visible_comment_count


class PostList(HomeFeedView):
    model = BlogPost
    paginate_by = 4
    context_object_name = "posts"
    feed_content_template_name = "blog/content.html"
    url_name = "blog_post_list"

    def get(self, request, *args, **kwargs):
        self.feed_type = request.GET.get("feed_type", "official")
        self.sort_by = request.GET.get("sort_by", "newest")
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        queryset = BlogPost.objects.filter(visible=True, publish_on__lte=timezone.now())

        if self.request.organization:
            queryset = queryset.filter(organizations=self.request.organization)

        if self.feed_type == "official":
            if not self.request.organization:
                queryset = queryset.filter(is_organization_private=False)
            if self.sort_by == "newest":
                queryset = queryset.order_by("-sticky", "-publish_on")
        elif self.feed_type == "group":
            if self.request.user.is_authenticated:
                if not self.request.organization:
                    queryset = queryset.filter(
                        is_organization_private=True,
                        organizations__in=self.request.profile.get_organization_ids(),
                    )
                if self.sort_by == "newest":
                    queryset = queryset.order_by("-publish_on")
            else:
                queryset = queryset.none()
        elif self.feed_type == "open_group":
            if not self.request.organization:
                queryset = queryset.filter(
                    is_organization_private=True,
                    organizations__is_open=True,
                )
            if self.sort_by == "newest":
                queryset = queryset.order_by("-publish_on")

        if self.sort_by == "latest_comment":
            queryset = queryset.annotate(latest_comment=Max("comments__time")).order_by(
                "-latest_comment", "-publish_on"
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super(PostList, self).get_context_data(**kwargs)
        context["title"] = (
            self.title or _("Page %d of Posts") % context["page_obj"].number
        )
        context["page_type"] = "blog"
        context["feed_type"] = self.feed_type
        context["sort_by"] = self.sort_by
        context["show_organization_private_icon"] = True
        BlogPost.prefetch_organization_ids(*[post.id for post in context["posts"]])
        return context

    def get_feed_context(self, object_list):
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
                # Org admins can always edit
                if self.request.profile.can_edit_organization(org):
                    context["editable_orgs"].append(org)
                # Org moderators can always edit
                elif org.can_moderate(self.request.profile):
                    context["editable_orgs"].append(org)
                # Blog authors can edit only if post is not yet approved
                elif is_author and not self.object.visible:
                    context["editable_orgs"].append(org)

        # Add ticket count for editors
        if self.object.is_editable_by(self.request.user):
            context["num_open_tickets"] = self.object.tickets.filter(
                is_open=True
            ).count()

        # Add comment context
        context = self.get_comment_context(context)

        return context

    def get_object(self, queryset=None):
        post = super(PostView, self).get_object(queryset)
        if not post.is_accessible_by(self.request.user):
            raise Http404()
        return post
