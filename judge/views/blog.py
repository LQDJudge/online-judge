from django.db.models import Count, Max, Q
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import lazy
from django.utils.translation import ugettext as _
from django.views.generic import ListView

from judge.comments import CommentedDetailView
from judge.views.pagevote import PageVoteDetailView
from judge.views.bookmark import BookMarkDetailView
from judge.models import (
    BlogPost,
    Comment,
    Contest,
    Language,
    Problem,
    ContestProblemClarification,
    Profile,
    Submission,
    Ticket,
)
from judge.models.profile import Organization, OrganizationProfile
from judge.utils.cachedict import CacheDict
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.problems import user_completed_ids
from judge.utils.tickets import filter_visible_tickets
from judge.utils.views import TitleMixin
from judge.views.feed import FeedView


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
            .filter(is_visible=True)
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
        context[
            "recent_organizations"
        ] = OrganizationProfile.get_most_recent_organizations(self.request.profile)

        profile_queryset = Profile.objects
        if self.request.organization:
            profile_queryset = self.request.organization.members
        context["top_rated"] = profile_queryset.filter(is_unlisted=False).order_by(
            "-rating"
        )[:10]
        context["top_scorer"] = profile_queryset.filter(is_unlisted=False).order_by(
            "-performance_points"
        )[:10]

        return context


class PostList(HomeFeedView):
    model = BlogPost
    paginate_by = 4
    context_object_name = "posts"
    feed_content_template_name = "blog/content.html"
    url_name = "blog_post_list"

    def get_queryset(self):
        queryset = (
            BlogPost.objects.filter(visible=True, publish_on__lte=timezone.now())
            .order_by("-sticky", "-publish_on")
            .prefetch_related("authors__user", "organizations")
        )
        filter = Q(is_organization_private=False)
        if self.request.user.is_authenticated:
            filter |= Q(organizations__in=self.request.profile.organizations.all())
        if self.request.organization:
            filter &= Q(organizations=self.request.organization)
        queryset = queryset.filter(filter)
        return queryset

    def get_feed_context(self, object_list):
        post_comment_counts = {
            int(page[2:]): count
            for page, count in Comment.objects.filter(
                page__in=["b:%d" % post.id for post in object_list], hidden=False
            )
            .values_list("page")
            .annotate(count=Count("page"))
            .order_by()
        }
        return {"post_comment_counts": post_comment_counts}

    def get_context_data(self, **kwargs):
        context = super(PostList, self).get_context_data(**kwargs)
        context["title"] = (
            self.title or _("Page %d of Posts") % context["page_obj"].number
        )
        context["page_type"] = "blog"
        context["post_comment_counts"] = {
            int(page[2:]): count
            for page, count in Comment.objects.filter(
                page__in=["b:%d" % post.id for post in context["posts"]], hidden=False
            )
            .values_list("page")
            .annotate(count=Count("page"))
            .order_by()
        }
        return context

    def get_comment_page(self, post):
        return "b:%s" % post.id


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
                    .select_related("user__user")
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
                    .select_related("user__user")
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
    paginate_by = 8
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


class PostView(TitleMixin, CommentedDetailView, PageVoteDetailView, BookMarkDetailView):
    model = BlogPost
    pk_url_kwarg = "id"
    context_object_name = "post"
    template_name = "blog/blog.html"

    def get_title(self):
        return self.object.title

    def get_comment_page(self):
        return "b:%s" % self.object.id

    def get_context_data(self, **kwargs):
        context = super(PostView, self).get_context_data(**kwargs)
        context["og_image"] = self.object.og_image
        context["valid_user_to_show_edit"] = False
        context["valid_org_to_show_edit"] = []

        if self.request.profile in self.object.authors.all():
            context["valid_user_to_show_edit"] = True

        for valid_org_to_show_edit in self.object.organizations.all():
            if self.request.profile in valid_org_to_show_edit.admins.all():
                context["valid_user_to_show_edit"] = True

        if context["valid_user_to_show_edit"]:
            for post_org in self.object.organizations.all():
                if post_org in self.request.profile.organizations.all():
                    context["valid_org_to_show_edit"].append(post_org)

        return context

    def get_object(self, queryset=None):
        post = super(PostView, self).get_object(queryset)
        if not post.can_see(self.request.user):
            raise Http404()
        return post
