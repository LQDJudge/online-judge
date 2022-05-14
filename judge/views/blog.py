from django.conf import settings
from django.db.models import Count, Max, Q
from django.http import Http404
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import lazy
from django.utils.translation import ugettext as _
from django.views.generic import ListView

from judge.comments import CommentedDetailView
from judge.models import (
    BlogPost,
    Comment,
    Contest,
    Language,
    Problem,
    ProblemClarification,
    Profile,
    Submission,
    Ticket,
)
from judge.utils.cachedict import CacheDict
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.problems import user_completed_ids
from judge.utils.tickets import filter_visible_tickets
from judge.utils.views import TitleMixin


# General view for all content list on home feed
class FeedView(ListView):
    template_name = "blog/list.html"
    title = None

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
            **kwargs
        )

    def get_context_data(self, **kwargs):
        context = super(FeedView, self).get_context_data(**kwargs)
        context["has_clarifications"] = False
        if self.request.user.is_authenticated:
            participation = self.request.profile.current_contest
            if participation:
                clarifications = ProblemClarification.objects.filter(
                    problem__in=participation.contest.problems.all()
                )
                context["has_clarifications"] = clarifications.count() > 0
                context["clarifications"] = clarifications.order_by("-date")
                if participation.contest.is_editable_by(self.request.user):
                    context["can_edit_contest"] = True

        context["page_titles"] = CacheDict(lambda page: Comment.get_page_title(page))

        context["user_count"] = lazy(Profile.objects.count, int, int)
        context["problem_count"] = lazy(
            Problem.objects.filter(is_public=True).count, int, int
        )
        context["submission_count"] = lazy(Submission.objects.count, int, int)
        context["language_count"] = lazy(Language.objects.count, int, int)

        now = timezone.now()

        # Dashboard stuff
        # if self.request.user.is_authenticated:
        #     user = self.request.profile
        #     context['recently_attempted_problems'] = (Submission.objects.filter(user=user)
        #                                               .exclude(problem__in=user_completed_ids(user))
        #                                               .values_list('problem__code', 'problem__name', 'problem__points')
        #                                               .annotate(points=Max('points'), latest=Max('date'))
        #                                               .order_by('-latest')
        #                                               [:settings.DMOJ_BLOG_RECENTLY_ATTEMPTED_PROBLEMS_COUNT])

        visible_contests = (
            Contest.get_visible_contests(self.request.user)
            .filter(is_visible=True)
            .order_by("start_time")
        )

        context["current_contests"] = visible_contests.filter(
            start_time__lte=now, end_time__gt=now
        )
        context["future_contests"] = visible_contests.filter(start_time__gt=now)

        visible_contests = Contest.get_visible_contests(self.request.user).filter(
            is_visible=True
        )

        context["top_rated"] = Profile.objects.filter(is_unlisted=False).order_by(
            "-rating"
        )[:10]
        context["top_scorer"] = Profile.objects.filter(is_unlisted=False).order_by(
            "-performance_points"
        )[:10]

        return context


class PostList(FeedView):
    model = BlogPost
    paginate_by = 10
    context_object_name = "posts"

    def get_queryset(self):
        queryset = (
            BlogPost.objects.filter(visible=True, publish_on__lte=timezone.now())
            .order_by("-sticky", "-publish_on")
            .prefetch_related("authors__user", "organizations")
        )
        if not self.request.user.has_perm("judge.edit_all_post"):
            filter = Q(is_organization_private=False)
            if self.request.user.is_authenticated:
                filter |= Q(organizations__in=self.request.profile.organizations.all())
            queryset = queryset.filter(filter)
        return queryset

    def get_context_data(self, **kwargs):
        context = super(PostList, self).get_context_data(**kwargs)
        context["title"] = (
            self.title or _("Page %d of Posts") % context["page_obj"].number
        )
        context["first_page_href"] = reverse("home")
        context["page_prefix"] = reverse("blog_post_list")
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


class TicketFeed(FeedView):
    model = Ticket
    context_object_name = "tickets"
    paginate_by = 30

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
        context["first_page_href"] = self.request.path
        context["page_prefix"] = "?page="
        context["title"] = _("Ticket feed")

        return context


class CommentFeed(FeedView):
    model = Comment
    context_object_name = "comments"
    paginate_by = 50

    def get_queryset(self):
        return Comment.most_recent(self.request.user, 1000)

    def get_context_data(self, **kwargs):
        context = super(CommentFeed, self).get_context_data(**kwargs)
        context["page_type"] = "comment"
        context["first_page_href"] = self.request.path
        context["page_prefix"] = "?page="
        context["title"] = _("Comment feed")

        return context


class PostView(TitleMixin, CommentedDetailView):
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
        return context

    def get_object(self, queryset=None):
        post = super(PostView, self).get_object(queryset)
        if not post.can_see(self.request.user):
            raise Http404()
        return post
