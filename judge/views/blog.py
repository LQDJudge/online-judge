from django.db.models import Count, Max, Q, Case, When
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import lazy
from django.utils.translation import gettext as _
from django.views.generic import ListView
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from django.views.generic import View
from django.contrib.contenttypes.models import ContentType

from judge.views.comment import CommentableMixin
from judge.views.pagevote import PageVoteDetailView
from judge.views.bookmark import BookMarkDetailView
from judge.models import (
    BlogPost,
    Comment,
    Contest,
    ContestProblemClarification,
    Profile,
    Ticket,
)
from judge.models.profile import (
    Organization,
    OrganizationProfile,
    get_top_rating_profile,
    get_top_score_profile,
)
from judge.utils.cachedict import CacheDict
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.tickets import filter_visible_tickets
from judge.utils.views import TitleMixin
from judge.utils.users import get_rating_rank, get_points_rank, get_awards
from judge.views.feed import FeedView
from judge.models.comment import get_visible_comment_count


# General view for all content list on home feed
class HomeFeedView(FeedView):
    template_name = "blog/list.html"
    title = None

    def get_context_data(self, **kwargs):
        context = super(HomeFeedView, self).get_context_data(**kwargs)
        context["has_clarifications"] = False
        if self.request.user.is_authenticated:
            participation = self.request.profile.current_contest
            if participation:
                clarifications = ContestProblemClarification.objects.filter(
                    problem__in=participation.contest.contest_problems.all()
                )
                context["has_clarifications"] = clarifications.count() > 0
                context["clarifications"] = clarifications.order_by("-date")
                if participation.contest.is_editable_by(self.request.user):
                    context["can_edit_contest"] = True

        now = timezone.now()
        visible_contests = (
            Contest.get_visible_contests(self.request.user, show_own_contests_only=True)
            .filter(is_visible=True, official__isnull=True)
            .order_by("start_time")
        )
        if self.request.organization:
            visible_contests = visible_contests.filter(
                is_organization_private=True, organizations=self.request.organization
            )
        context["current_contests"] = visible_contests.filter(
            start_time__lte=now, end_time__gt=now
        )
        context["future_contests"] = visible_contests.filter(start_time__gt=now)
        context["recent_organizations"] = (
            OrganizationProfile.get_most_recent_organizations(self.request.profile)
        )

        context["top_rated"] = get_top_rating_profile(
            self.request.organization.id if self.request.organization else None
        )
        context["top_scorer"] = get_top_score_profile(
            self.request.organization.id if self.request.organization else None
        )

        if self.request.user.is_authenticated:
            context["rating_rank"] = get_rating_rank(self.request.profile)
            context["points_rank"] = get_points_rank(self.request.profile)

            medals_list = get_awards(self.request.profile)
            context["awards"] = {
                "medals": medals_list,
                "gold_count": 0,
                "silver_count": 0,
                "bronze_count": 0,
            }
            for medal in medals_list:
                if medal["ranking"] == 1:
                    context["awards"]["gold_count"] += 1
                elif medal["ranking"] == 2:
                    context["awards"]["silver_count"] += 1
                elif medal["ranking"] == 3:
                    context["awards"]["bronze_count"] += 1

        return context


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


class TicketFeed(HomeFeedView):
    model = Ticket
    context_object_name = "tickets"
    paginate_by = 8
    feed_content_template_name = "ticket/feed.html"

    def get_queryset(self, is_own=True):
        profile = self.request.profile
        if is_own:
            if self.request.user.is_authenticated:
                return (
                    Ticket.objects.filter(
                        Q(user=profile) | Q(assignees__in=[profile]), is_open=True
                    )
                    .order_by("-id")
                    .prefetch_related("linked_item")
                )
            else:
                return []
        else:
            # Superusers better be staffs, not the spell-casting kind either.
            if self.request.user.is_staff:
                tickets = (
                    Ticket.objects.order_by("-id")
                    .filter(is_open=True)
                    .prefetch_related("linked_item")
                )
                return filter_visible_tickets(tickets, self.request.user, profile)
            else:
                return []

    def get_context_data(self, **kwargs):
        context = super(TicketFeed, self).get_context_data(**kwargs)
        context["page_type"] = "ticket"
        context["title"] = _("Ticket feed")
        return context


class CommentFeed(HomeFeedView):
    model = Comment
    context_object_name = "comments"
    paginate_by = 15
    feed_content_template_name = "comments/feed.html"

    def get_queryset(self):
        return Comment.most_recent(
            self.request.user, 100, organization=self.request.organization
        )

    def get_context_data(self, **kwargs):
        context = super(CommentFeed, self).get_context_data(**kwargs)
        context["title"] = _("Comment feed")
        context["page_type"] = "comment"
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
            for org in context["organizations"]:
                if self.request.profile.can_edit_organization(org):
                    context["editable_orgs"].append(org)

        # Add comment context
        context = self.get_comment_context(context)

        return context

    def get_object(self, queryset=None):
        post = super(PostView, self).get_object(queryset)
        if not post.is_accessible_by(self.request.user):
            raise Http404()
        return post
