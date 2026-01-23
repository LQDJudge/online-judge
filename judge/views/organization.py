from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.text import slugify
from django.db import IntegrityError
from django.db.models import Count, Q, Subquery, OuterRef
from django.forms import Form, modelformset_factory
from django.http import (
    Http404,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy, ngettext
from django.views.generic import (
    DetailView,
    FormView,
    ListView,
    UpdateView,
    View,
    CreateView,
)
from django.views.generic.detail import (
    SingleObjectMixin,
    SingleObjectTemplateResponseMixin,
)
from django.core.paginator import Paginator
from django.contrib.sites.shortcuts import get_current_site
from django.urls.exceptions import NoReverseMatch
from reversion import revisions

from judge.forms import (
    EditOrganizationForm,
    AddOrganizationForm,
    AddOrganizationMemberForm,
    OrganizationBlogForm,
    OrganizationAdminBlogForm,
    EditOrganizationContestForm,
    ContestProblemFormSet,
    ContestQuizFormSet,
    AddOrganizationContestForm,
)
from judge.models import (
    BlogPost,
    Comment,
    CommentVote,
    Organization,
    OrganizationRequest,
    OrganizationModerationLog,
    Profile,
    Contest,
    ContestProblem,
    OrganizationProfile,
    Block,
    Course,
    CourseRole,
    PageVote,
    PageVoteVoter,
)
from judge.models.course import RoleInCourse
from judge.models.notification import Notification, NotificationCategory
from judge.models.block import get_all_blocked_pairs
from judge import event_poster as event
from judge.utils.ranker import ranker
from judge.utils.views import (
    TitleMixin,
    generic_message,
    QueryStringSortMixin,
    DiggPaginatorMixin,
    SingleObjectFormView,
)
from judge.utils.problems import user_attempted_ids, user_completed_ids
from judge.utils.contest import maybe_trigger_contest_rescore
from judge.views.problem import ProblemList
from judge.views.contests import ContestList
from judge.views.course import CourseList
from judge.views.submission import SubmissionsListBase
from judge.views.feed import FeedView
from judge.models.profile import get_top_rating_profile, get_top_score_profile
from judge.caching import cache_wrapper
from collections import defaultdict


@cache_wrapper(prefix="Pgtcpi4", timeout=1800, expected_type=list)
def _get_top_contributors_inner(organization_id):
    """
    Calculate contribution scores for users in a community.
    All contributions are credited to authors (post authors get post votes, comment authors get comment votes).
    Score = (posts * 3) + (post_votes_received * 3) + (comments * 1) + (comment_votes_received * 1)
    """
    scores = defaultdict(
        lambda: {"posts": 0, "post_votes": 0, "comments": 0, "comment_votes": 0}
    )

    # Get blog posts in this organization with their authors
    blog_posts = BlogPost.objects.filter(
        organizations=organization_id, visible=True
    ).values("id", "authors")

    if not blog_posts:
        return []

    blog_post_ids = [p["id"] for p in blog_posts]
    # Map post_id -> author_id
    post_author_map = {p["id"]: p["authors"] for p in blog_posts if p["authors"]}

    blog_content_type = ContentType.objects.get_for_model(BlogPost)

    # Count blog posts per author (3 points each)
    for author_id in post_author_map.values():
        scores[author_id]["posts"] += 1

    # Count post votes and credit to POST AUTHORS (3 points each)
    pagevotes = PageVote.objects.filter(
        content_type=blog_content_type,
        object_id__in=blog_post_ids,
    ).values("id", "object_id")

    pagevote_to_post = {pv["id"]: pv["object_id"] for pv in pagevotes}
    if pagevote_to_post:
        vote_counts = (
            PageVoteVoter.objects.filter(pagevote_id__in=pagevote_to_post.keys())
            .values("pagevote_id")
            .annotate(count=Count("id"))
        )
        for item in vote_counts:
            post_id = pagevote_to_post[item["pagevote_id"]]
            author_id = post_author_map.get(post_id)
            if author_id:
                scores[author_id]["post_votes"] += item["count"]

    # Get all comments with authors (single query for both counting and vote mapping)
    comments = list(
        Comment.objects.filter(
            content_type=blog_content_type,
            object_id__in=blog_post_ids,
        ).values("id", "author", "hidden")
    )

    # Count comments per author (1 point each) - only visible comments
    for c in comments:
        if c["author"] and not c["hidden"]:
            scores[c["author"]]["comments"] += 1

    # Build comment_id -> author map for vote attribution
    comment_author_map = {c["id"]: c["author"] for c in comments if c["author"]}
    if comment_author_map:
        vote_counts = (
            CommentVote.objects.filter(comment_id__in=comment_author_map.keys())
            .values("comment_id")
            .annotate(count=Count("id"))
        )
        for item in vote_counts:
            author_id = comment_author_map.get(item["comment_id"])
            if author_id:
                scores[author_id]["comment_votes"] += item["count"]

    # Calculate total scores: posts*3 + post_votes*3 + comments*1 + comment_votes*1
    results = []
    for profile_id, data in scores.items():
        total = (
            data["posts"] * 3
            + data["post_votes"] * 3
            + data["comments"] * 1
            + data["comment_votes"] * 1
        )
        if total > 0:
            results.append(
                (
                    profile_id,
                    total,
                    data["posts"],
                    data["post_votes"],
                    data["comments"],
                    data["comment_votes"],
                )
            )

    # Sort by total score descending
    results.sort(key=lambda x: -x[1])
    return results[:10]


def get_top_contributors(organization_id):
    """Get top contributors for a community organization"""
    results = _get_top_contributors_inner(organization_id)
    if not results:
        return []

    profile_ids = [r[0] for r in results]
    score_data = {
        r[0]: {
            "score": r[1],
            "posts": r[2],
            "post_votes": r[3],
            "comments": r[4],
            "comment_votes": r[5],
        }
        for r in results
    }

    profiles = Profile.get_cached_instances(*profile_ids)
    # Attach contribution data to each profile for template use
    for profile in profiles:
        data = score_data.get(profile.id, {})
        profile.contribution_score = data.get("score", 0)
        profile.post_count = data.get("posts", 0)
        profile.post_vote_count = data.get("post_votes", 0)
        profile.comment_count = data.get("comments", 0)
        profile.comment_vote_count = data.get("comment_votes", 0)
    return profiles


def _attach_rejection_info(blogs, organization, model_class):
    """
    Attach rejection info from moderation logs to a list of blog posts.
    Each blog will have a rejection_info attribute with moderator, reason, etc.
    """
    if not blogs:
        return

    content_type = ContentType.objects.get_for_model(model_class)
    blog_ids = [blog.id for blog in blogs]

    # Get the most recent reject_post action for each blog
    rejection_logs = (
        OrganizationModerationLog.objects.filter(
            organization=organization,
            content_type=content_type,
            object_id__in=blog_ids,
            action="reject_post",
        )
        .order_by("-created_at")
        .select_related("moderator")
    )

    # Build a map of blog_id -> rejection info (most recent only)
    rejection_info = {}
    for log in rejection_logs:
        if log.object_id not in rejection_info:
            rejection_info[log.object_id] = {
                "moderator": log.moderator,
                "reason": log.reason,
                "created_at": log.created_at,
                "is_automated": log.is_automated,
            }

    # Attach to each blog
    for blog in blogs:
        blog.rejection_info = rejection_info.get(blog.id)


__all__ = [
    "OrganizationList",
    "OrganizationHome",
    "OrganizationUsers",
    "OrganizationProblems",
    "OrganizationContests",
    "OrganizationMembershipChange",
    "JoinOrganization",
    "LeaveOrganization",
    "EditOrganization",
    "RequestJoinOrganization",
    "OrganizationRequestDetail",
    "OrganizationRequestView",
    "OrganizationRequestLog",
    "KickUserWidgetView",
    "OrganizationCourses",
]


class OrganizationBase(object):
    def can_edit_organization(self, org=None):
        if org is None:
            org = self.object
        if self.request.profile:
            return self.request.profile.can_edit_organization(org)
        return False

    def is_member(self, org=None):
        if org is None:
            org = self.object
        if self.request.profile:
            return org.is_member(self.request.profile)
        return False

    def is_admin(self, org=None):
        if org is None:
            org = self.object
        if self.request.profile:
            return org.is_admin(self.request.profile)
        return False

    def is_blocked(self, org=None):
        if org is None:
            org = self.object
        if self.request.profile:
            block = Block()
            return block.is_blocked(self.request.profile, org)
        return False

    def can_access(self, org):
        if self.request.user.is_superuser:
            return True
        if org is None:
            org = self.object
        return self.is_member(org) or self.can_edit_organization(org)


class OrganizationMixin(OrganizationBase):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["is_member"] = self.is_member(self.organization)
        context["is_admin"] = self.is_admin(self.organization)
        context["is_moderator"] = (
            self.request.profile
            and self.organization.can_moderate(self.request.profile)
        )
        context["is_blocked"] = self.is_blocked(self.organization)
        context["can_edit"] = self.can_edit_organization(self.organization)
        context["organization"] = self.organization
        context["organization_image"] = self.organization.organization_image
        context["cover_image"] = self.organization.cover_image
        context["organization_subdomain"] = (
            ("http" if settings.DMOJ_SSL == 0 else "https")
            + "://"
            + self.organization.slug
            + "."
            + get_current_site(self.request).domain
        )
        if "organizations" in context:
            context.pop("organizations")
        return context

    def dispatch(self, request, *args, **kwargs):
        try:
            self.organization_id = int(kwargs["pk"])
            self.organization = get_object_or_404(Organization, id=self.organization_id)
        except Http404:
            key = None
            if hasattr(self, "slug_url_kwarg"):
                key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(
                    request,
                    _("No such organization"),
                    _("Could not find an organization with the key %s.") % key,
                    status=403,
                )
            else:
                return generic_message(
                    request,
                    _("No such organization"),
                    _("Could not find such organization."),
                    status=403,
                )
        if self.organization.slug != kwargs["slug"]:
            return HttpResponsePermanentRedirect(
                request.get_full_path().replace(kwargs["slug"], self.organization.slug)
            )
        if self.request.user.is_authenticated:
            OrganizationProfile.add_organization(
                self.request.profile, self.organization
            )

        return super(OrganizationMixin, self).dispatch(request, *args, **kwargs)


class AdminOrganizationMixin(OrganizationMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(AdminOrganizationMixin, self).dispatch(request, *args, **kwargs)
        if not hasattr(self, "organization") or self.can_edit_organization(
            self.organization
        ):
            return res
        return generic_message(
            request,
            _("Can't edit organization"),
            _("You are not allowed to edit this organization."),
            status=403,
        )


class MemberOrganizationMixin(OrganizationMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(MemberOrganizationMixin, self).dispatch(request, *args, **kwargs)
        if not hasattr(self, "organization") or self.can_access(self.organization):
            return res
        return generic_message(
            request,
            _("Can't access organization"),
            _("You are not allowed to access this organization."),
            status=403,
        )


class CommunityOrMemberMixin(OrganizationMixin):
    """
    Mixin that allows access if:
    - The organization is a community (anyone can access), OR
    - The user is a member/admin of the organization
    """

    def dispatch(self, request, *args, **kwargs):
        res = super(CommunityOrMemberMixin, self).dispatch(request, *args, **kwargs)
        if not hasattr(self, "organization"):
            return res
        # Allow access if it's a community or if user can access
        if self.organization.is_community or self.can_access(self.organization):
            return res
        return generic_message(
            request,
            _("Can't access organization"),
            _("You are not allowed to access this organization."),
            status=403,
        )


class OrganizationHomeView(OrganizationMixin):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not hasattr(self, "organization"):
            self.organization = self.object
        if self.can_edit_organization(self.organization):
            context["pending_count"] = OrganizationRequest.objects.filter(
                state="P", organization=self.organization
            ).count()
            context["pending_blog_count"] = BlogPost.objects.filter(
                visible=False, organizations=self.organization, is_rejected=False
            ).count()
        else:
            context["pending_blog_count"] = BlogPost.objects.filter(
                visible=False,
                organizations=self.organization,
                authors=self.request.profile,
                is_rejected=False,
            ).count()
        # Communities show top contributors instead of top rated/scorer
        if self.organization.is_community:
            context["top_contributors"] = get_top_contributors(self.organization.id)
        else:
            context["top_rated"] = get_top_rating_profile(self.organization.id)
            context["top_scorer"] = get_top_score_profile(self.organization.id)

        return context


class OrganizationList(
    QueryStringSortMixin, DiggPaginatorMixin, TitleMixin, ListView, OrganizationBase
):
    model = Organization
    context_object_name = "organizations"
    template_name = "organization/list.html"
    title = gettext_lazy("Groups")
    paginate_by = 12
    all_sorts = frozenset(("name", "member_count", "last_visit"))
    default_desc = frozenset(("name", "member_count", "last_visit"))

    def get_default_sort_order(self, request):
        # Default to last visit time for "mine" and "public", member count for "private"
        if self.current_tab in ("public", "mine") and self.request.profile:
            return "-last_visit"
        return "-member_count"

    def get(self, request, *args, **kwargs):
        default_tab = "community"
        self.current_tab = self.request.GET.get("tab", default_tab)
        self.organization_query = request.GET.get("organization", "")

        # Handle order parameter validation
        order = request.GET.get("order", "")

        # If user is not authenticated and tries to use last_visit ordering,
        # fallback to default ordering
        if not self.request.user.is_authenticated and order.lstrip("-") == "last_visit":
            order = self.get_default_sort_order(request)

        # Validate order parameter against available sorts
        if not (
            (not order.startswith("-") or order.count("-") == 1)
            and (order.lstrip("-") in self.all_sorts)
        ):
            order = self.get_default_sort_order(request)

        self.order = order

        # Call ListView.get() directly to skip QueryStringSortMixin.get()
        # since we've already handled the order validation above
        return super(QueryStringSortMixin, self).get(request, *args, **kwargs)

    def _get_queryset(self):
        profile = self.request.profile

        # Join with OrganizationProfile to get the last visit time
        if profile:
            queryset = (
                super(OrganizationList, self)
                .get_queryset()
                .annotate(member_count=Count("member"))
                .annotate(
                    last_visit=Subquery(
                        OrganizationProfile.objects.filter(
                            profile=profile, organization_id=OuterRef("id")
                        ).values("last_visit_time")[:1]
                    )
                )
                .defer("about")
            )
        else:
            queryset = (
                super(OrganizationList, self)
                .get_queryset()
                .annotate(member_count=Count("member"))
                .defer("about")
            )

        if self.organization_query:
            queryset = queryset.filter(
                Q(slug__icontains=self.organization_query)
                | Q(name__icontains=self.organization_query)
                | Q(short_name__icontains=self.organization_query)
            )
        return queryset

    def get_queryset(self):
        organization_list = self._get_queryset()

        profile = self.request.profile
        organization_type = ContentType.objects.get_for_model(Organization)

        blocked_organization_ids = set()
        if profile:
            blocked_pairs = get_all_blocked_pairs(profile)
            blocked_organization_ids = {
                blocked_id
                for blocked_type, blocked_id in blocked_pairs
                if blocked_type == organization_type.id
            }

        my_organizations = []
        if profile:
            my_organizations = organization_list.filter(
                id__in=profile.organizations.values("id")
            ).exclude(id__in=blocked_organization_ids)

        if self.current_tab == "community":
            queryset = organization_list.filter(is_community=True).exclude(
                id__in=blocked_organization_ids
            )
        elif self.current_tab == "public":
            queryset = organization_list.exclude(
                Q(id__in=my_organizations) | Q(id__in=blocked_organization_ids)
            ).filter(is_open=True)
        elif self.current_tab == "private":
            queryset = organization_list.exclude(
                Q(id__in=my_organizations) | Q(id__in=blocked_organization_ids)
            ).filter(is_open=False)
        elif self.current_tab == "blocked":
            queryset = organization_list.filter(id__in=blocked_organization_ids)
        else:
            # "mine" tab - all joined groups including communities
            queryset = my_organizations

        if queryset:
            # Sort communities first, then apply the user's sort order
            queryset = queryset.order_by("-is_community", self.order)

        return queryset

    def get_context_data(self, **kwargs):
        context = super(OrganizationList, self).get_context_data(**kwargs)

        context["current_tab"] = self.current_tab
        context["page_type"] = self.current_tab
        context["organization_query"] = self.organization_query
        context["selected_order"] = self.request.GET.get(
            "order", self.get_default_sort_order(self.request)
        )
        context["all_sort_options"] = [
            ("name", _("Name (asc.)")),
            ("-name", _("Name (desc.)")),
            ("member_count", _("Member count (asc.)")),
            ("-member_count", _("Member count (desc.)")),
        ]

        # Only add last visit options if user is authenticated
        if self.request.user.is_authenticated:
            context["all_sort_options"].extend(
                [
                    ("-last_visit", _("Last visit")),
                ]
            )

        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())

        return context


class OrganizationHome(OrganizationHomeView, FeedView):
    template_name = "organization/home.html"
    paginate_by = 4
    context_object_name = "posts"
    feed_content_template_name = "blog/content.html"

    def get_queryset(self):
        return BlogPost.objects.filter(
            visible=True,
            publish_on__lte=timezone.now(),
            is_organization_private=True,
            organizations=self.organization,
        ).order_by("-sticky", "-publish_on")

    def get_context_data(self, **kwargs):
        context = super(OrganizationHome, self).get_context_data(**kwargs)
        context["title"] = self.organization.name

        now = timezone.now()
        visible_contests = (
            Contest.get_visible_contests(self.request.user)
            .filter(
                is_visible=True,
                is_organization_private=True,
                organizations=self.organization,
            )
            .order_by("start_time")
        )
        context["current_contests"] = visible_contests.filter(
            start_time__lte=now, end_time__gt=now
        )
        context["future_contests"] = visible_contests.filter(start_time__gt=now)
        context["page_type"] = "home"

        # Stats for header (using cached member IDs)
        member_ids = self.organization.get_member_ids()
        context["member_count"] = len(member_ids)

        # Member avatars for preview (up to 5, using cached instances)
        preview_ids = member_ids[:5]
        context["member_preview"] = Profile.get_cached_instances(*preview_ids)

        return context


class OrganizationUsers(
    DiggPaginatorMixin, QueryStringSortMixin, OrganizationMixin, ListView
):
    template_name = "organization/users.html"
    all_sorts = frozenset(("points", "problem_count", "rating", "performance_points"))
    default_desc = all_sorts
    default_sort = "-performance_points"
    paginate_by = 100
    context_object_name = "users"

    def get_queryset(self):
        return (
            self.organization.members.filter(is_unlisted=False)
            .order_by(self.order, "id")
            .select_related("user")
            .only(
                "display_rank",
                "user__username",
                "points",
                "rating",
                "performance_points",
                "problem_count",
            )
        )

    def dispatch(self, request, *args, **kwargs):
        res = super(OrganizationUsers, self).dispatch(request, *args, **kwargs)
        if res.status_code != 200:
            return res
        if self.can_access(self.organization) or self.organization.is_open:
            return res
        return generic_message(
            request,
            _("Can't access organization"),
            _("You are not allowed to access this organization."),
            status=403,
        )

    def get_context_data(self, **kwargs):
        context = super(OrganizationUsers, self).get_context_data(**kwargs)
        context["title"] = _("%s Members") % self.organization.name
        context["partial"] = True
        context["kick_url"] = reverse(
            "organization_user_kick",
            args=[self.organization.id, self.organization.slug],
        )
        context["users"] = ranker(
            context["users"], rank=self.paginate_by * (context["page_obj"].number - 1)
        )

        context["page_type"] = "users"
        context.update(self.get_sort_context())
        return context


class OrganizationProblems(LoginRequiredMixin, MemberOrganizationMixin, ProblemList):
    template_name = "organization/problems.html"

    def get_queryset(self):
        self.org_query = [self.organization_id]
        return super().get_normal_queryset()

    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)
        return super().get(request, *args, **kwargs)

    def get_completed_problems(self):
        return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_attempted_problems(self):
        return user_attempted_ids(self.profile) if self.profile is not None else ()

    @cached_property
    def in_contest(self):
        return False

    def get_context_data(self, **kwargs):
        context = super(OrganizationProblems, self).get_context_data(**kwargs)
        context["page_type"] = "problems"
        context["show_contest_mode"] = False
        return context


class OrganizationContestMixin(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
):
    model = Contest

    def is_contest_editable(self, request, contest):
        return contest.is_editable_by(request.user) or self.can_edit_organization(
            self.organization
        )


class OrganizationCourseMixin(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
):
    model = Course

    def is_course_editable(self, request, course):
        """Check if course is editable by current user or organization admin"""
        return Course.is_editable_by(
            course, request.profile
        ) or self.can_edit_organization(self.organization)


class OrganizationContests(
    OrganizationContestMixin, MemberOrganizationMixin, ContestList
):
    template_name = "organization/contests.html"

    def get_queryset(self):
        self.org_query = [self.organization_id]
        self.hide_organization_contests = False
        return super().get_queryset()

    def set_editable_contest(self, contest):
        if not contest:
            return False
        contest.is_editable = self.is_contest_editable(self.request, contest)

    def get_context_data(self, **kwargs):
        context = super(OrganizationContests, self).get_context_data(**kwargs)
        context["page_type"] = "contests"
        context.pop("organizations")

        if self.can_edit_organization(self.organization):
            context["create_url"] = reverse(
                "organization_contest_add",
                args=[self.organization.id, self.organization.slug],
            )

        if self.current_tab == "active":
            for participation in context["contests"]:
                self.set_editable_contest(participation.contest)
        else:
            for contest in context["contests"]:
                self.set_editable_contest(contest)
        return context


class OrganizationSubmissions(
    LoginRequiredMixin, MemberOrganizationMixin, SubmissionsListBase
):
    template_name = "organization/submissions.html"

    @cached_property
    def in_contest(self):
        return False

    @cached_property
    def contest(self):
        return None

    def get_context_data(self, **kwargs):
        context = super(OrganizationSubmissions, self).get_context_data(**kwargs)
        # context["dynamic_update"] = context["page_obj"].number == 1
        # context["last_msg"] = event.last()
        context["stats_update_interval"] = 3600
        context["page_type"] = "submissions"

        return context

    def get_content_title(self):
        return format_html(
            _('All submissions in <a href="{1}">{0}</a>'),
            self.organization,
            reverse(
                "organization_home", args=[self.organization.id, self.organization.slug]
            ),
        )

    def get_title(self):
        return _("Submissions in") + f" {self.organization}"


class OrganizationMembershipChange(
    LoginRequiredMixin, OrganizationMixin, SingleObjectMixin, View
):
    model = Organization
    context_object_name = "organization"

    def post(self, request, *args, **kwargs):
        org = self.get_object()
        response = self.handle(request, org, request.profile)
        if response is not None:
            return response
        return HttpResponseRedirect(org.get_absolute_url())

    def handle(self, request, org, profile):
        raise NotImplementedError()


class JoinOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if profile.organizations.filter(id=org.id).exists():
            return generic_message(
                request,
                _("Joining group"),
                _("You are already in the group."),
            )

        if Block.is_blocked(blocker=profile, blocked=org):
            return generic_message(
                request,
                _("Joining group"),
                _("You cannot join since you have already blocked %s.")
                % org.short_name,
            )

        if not org.is_open:
            return generic_message(
                request, _("Joining group"), _("This group is not open.")
            )

        # Communities don't count towards the join limit
        if not org.is_community:
            max_orgs = settings.DMOJ_USER_MAX_ORGANIZATION_COUNT
            # Only count non-community open groups towards the limit
            current_count = profile.organizations.filter(
                is_open=True, is_community=False
            ).count()
            if current_count >= max_orgs:
                return generic_message(
                    request,
                    _("Joining group"),
                    _("You may not be part of more than {count} public groups.").format(
                        count=max_orgs
                    ),
                )

        profile.organizations.add(org)
        profile.save()


class LeaveOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if not profile.organizations.filter(id=org.id).exists():
            return generic_message(
                request,
                _("Leaving group"),
                _("You are not in %s.") % org.short_name,
            )
        profile.organizations.remove(org)


class BlockOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if Block.is_blocked(blocker=profile, blocked=org):
            return generic_message(
                request,
                _("Blocking group"),
                _("You have already blocked %s.") % org.short_name,
            )

        try:
            Block.add_block(blocker=profile, blocked=org)
        except Exception as e:
            return generic_message(
                request,
                _("Blocking group"),
                _("An error occurred while blocking %(org)s. Reason: %(reason)s")
                % {"org": org.short_name, "reason": str(e)},
            )

        if profile.organizations.filter(id=org.id).exists():
            profile.organizations.remove(org)

        return HttpResponseRedirect(reverse("organization_list") + "?tab=blocked")


class UnblockOrganization(OrganizationMembershipChange):
    def handle(self, request, org, profile):
        if not Block.is_blocked(blocker=profile, blocked=org):
            return generic_message(
                request,
                _("Blocking group"),
                _("You have not blocked %s.") % org.short_name,
            )

        try:
            Block.remove_block(blocker=profile, blocked=org)
        except Exception as e:
            return generic_message(
                request,
                _("Blocking group"),
                _("An error occurred while unblocking %(org)s. Reason: %(reason)s")
                % {"org": org.short_name, "reason": str(e)},
            )

        return HttpResponseRedirect(reverse("organization_list") + "?tab=blocked")


class OrganizationRequestForm(Form):
    reason = forms.CharField(widget=forms.Textarea)


class RequestJoinOrganization(LoginRequiredMixin, SingleObjectMixin, FormView):
    model = Organization
    slug_field = "key"
    slug_url_kwarg = "key"
    template_name = "organization/requests/request.html"
    form_class = OrganizationRequestForm

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()

        profile = self.request.profile
        org = self.get_object()

        if Block.is_blocked(blocker=profile, blocked=org):
            return generic_message(
                request,
                _("Request to join group"),
                _("You cannot request since you have already blocked %s.")
                % org.short_name,
            )

        return super(RequestJoinOrganization, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(RequestJoinOrganization, self).get_context_data(**kwargs)
        if self.object.is_open:
            raise Http404()
        context["title"] = _("Request to join %s") % self.object.name
        return context

    def form_valid(self, form):
        request = OrganizationRequest()
        request.organization = self.get_object()
        request.user = self.request.profile
        request.reason = form.cleaned_data["reason"]
        request.state = "P"
        request.save()
        return HttpResponseRedirect(
            reverse(
                "request_organization_detail",
                args=(
                    request.organization.id,
                    request.organization.slug,
                    request.id,
                ),
            )
        )


class OrganizationRequestDetail(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
    DetailView,
):
    model = OrganizationRequest
    template_name = "organization/requests/detail.html"
    title = gettext_lazy("Join request detail")
    pk_url_kwarg = "rpk"

    def get_object(self, queryset=None):
        object = super(OrganizationRequestDetail, self).get_object(queryset)
        profile = self.request.profile
        if (
            object.user_id != profile.id
            and not object.organization.admins.filter(id=profile.id).exists()
        ):
            raise PermissionDenied()
        return object


OrganizationRequestFormSet = modelformset_factory(
    OrganizationRequest, extra=0, fields=("state",), can_delete=True
)


class OrganizationRequestBaseView(
    AdminOrganizationMixin,
    DetailView,
    OrganizationHomeView,
    TitleMixin,
    LoginRequiredMixin,
    SingleObjectTemplateResponseMixin,
    SingleObjectMixin,
):
    model = Organization
    slug_field = "key"
    slug_url_kwarg = "key"
    tab = None

    def get_content_title(self):
        return _("Manage join requests")

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestBaseView, self).get_context_data(**kwargs)
        context["title"] = _("Managing join requests for %s") % self.object.name
        context["tab"] = self.tab
        return context


class OrganizationRequestView(OrganizationRequestBaseView):
    template_name = "organization/requests/pending.html"
    tab = "pending"

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestView, self).get_context_data(**kwargs)
        context["formset"] = self.formset
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        self.formset = OrganizationRequestFormSet(
            queryset=OrganizationRequest.objects.filter(
                state="P", organization=self.object
            ),
        )
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def post(self, request, *args, **kwargs):
        self.object = organization = self.get_object()
        self.formset = formset = OrganizationRequestFormSet(request.POST, request.FILES)
        if formset.is_valid():
            if organization.slots is not None:
                deleted_set = set(formset.deleted_forms)
                to_approve = sum(
                    form.cleaned_data["state"] == "A"
                    for form in formset.forms
                    if form not in deleted_set
                )
                can_add = organization.slots - organization.members.count()
                if to_approve > can_add:
                    messages.error(
                        request,
                        _(
                            "Your organization can only receive %(can_add)d more members. "
                            "You cannot approve %(to_approve)d users."
                        )
                        % {"can_add": can_add, "to_approve": to_approve},
                    )
                    return self.render_to_response(
                        self.get_context_data(object=organization)
                    )

            approved, rejected = 0, 0
            for obj in formset.save():
                if obj.state == "A":
                    obj.user.organizations.add(obj.organization)
                    approved += 1
                elif obj.state == "R":
                    rejected += 1
            messages.success(
                request,
                ngettext("Approved %d user.", "Approved %d users.", approved) % approved
                + "\n"
                + ngettext("Rejected %d user.", "Rejected %d users.", rejected)
                % rejected,
            )
            return HttpResponseRedirect(request.get_full_path())
        return self.render_to_response(self.get_context_data(object=organization))

    put = post


class OrganizationRequestLog(OrganizationRequestBaseView):
    states = ("A", "R")
    tab = "log"
    template_name = "organization/requests/log.html"

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super(OrganizationRequestLog, self).get_context_data(**kwargs)
        context["requests"] = self.object.requests.filter(state__in=self.states)
        return context


class AddOrganizationMember(
    LoginRequiredMixin,
    TitleMixin,
    AdminOrganizationMixin,
    OrganizationHomeView,
    UpdateView,
):
    template_name = "organization/add-member.html"
    model = Organization
    form_class = AddOrganizationMemberForm

    def get_title(self):
        return _("Add member for %s") % self.object.name

    def get_object(self, queryset=None):
        object = super(AddOrganizationMember, self).get_object()
        if not self.can_edit_organization(object):
            raise PermissionDenied()
        return object

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["organization"] = self.object
        return kwargs

    def form_valid(self, form):
        new_users = form.cleaned_data["new_users"]
        self.object.members.add(*new_users)
        link = reverse("organization_home", args=[self.object.id, self.object.slug])
        html = f'<a href="{link}">{self.object.name}</a>'
        Notification.objects.bulk_create_notifications(
            user_ids=[u.id for u in new_users],
            category=NotificationCategory.ORGANIZATION,
            html_link=html,
            author=self.request.profile,
        )
        with revisions.create_revision():
            usernames = ", ".join([u.username for u in new_users])
            revisions.set_comment(_("Added members from site") + ": " + usernames)
            revisions.set_user(self.request.user)
            return super(AddOrganizationMember, self).form_valid(form)

    def get_success_url(self):
        return reverse("organization_users", args=[self.object.id, self.object.slug])


class KickUserWidgetView(
    LoginRequiredMixin, AdminOrganizationMixin, SingleObjectMixin, View
):
    model = Organization

    def post(self, request, *args, **kwargs):
        organization = self.get_object()
        try:
            user = Profile.objects.get(id=request.POST.get("user", None))
        except Profile.DoesNotExist:
            return generic_message(
                request,
                _("Can't kick user"),
                _("The user you are trying to kick does not exist!"),
                status=400,
            )

        if not organization.is_member(user):
            return generic_message(
                request,
                _("Can't kick user"),
                _("The user you are trying to kick is not in group: %s.")
                % organization.name,
                status=400,
            )

        if organization.is_admin(user):
            return generic_message(
                request,
                _("Can't kick user"),
                _("The user you are trying to kick is a group admin."),
                status=400,
            )

        with revisions.create_revision():
            revisions.set_comment(_("Kicked member") + " " + user.username)
            revisions.set_user(self.request.user)
            organization.members.remove(user)
            organization.save()

        return HttpResponseRedirect(organization.get_users_url())


class EditOrganization(
    LoginRequiredMixin,
    TitleMixin,
    AdminOrganizationMixin,
    OrganizationHomeView,
    UpdateView,
):
    template_name = "organization/edit.html"
    model = Organization
    form_class = EditOrganizationForm

    def get_title(self):
        return _("Edit %s") % self.object.name

    def get_object(self, queryset=None):
        object = super(EditOrganization, self).get_object()
        if not self.can_edit_organization(object):
            raise PermissionDenied()
        return object

    def get_form_kwargs(self):
        kwargs = super(EditOrganization, self).get_form_kwargs()
        kwargs["org_id"] = self.organization.id
        return kwargs

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            return super(EditOrganization, self).form_valid(form)


class AddOrganization(LoginRequiredMixin, TitleMixin, CreateView):
    template_name = "organization/add.html"
    model = Organization
    form_class = AddOrganizationForm

    def get_title(self):
        return _("Create group")

    def get_form_kwargs(self):
        kwargs = super(AddOrganization, self).get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        if (
            not self.request.user.is_staff
            and Organization.objects.filter(registrant=self.request.profile).count()
            >= settings.DMOJ_USER_MAX_ORGANIZATION_ADD
        ):
            return generic_message(
                self.request,
                _("Exceeded limit"),
                _("You created too many groups. You can only create at most %d groups")
                % settings.DMOJ_USER_MAX_ORGANIZATION_ADD,
                status=400,
            )
        with revisions.create_revision():
            revisions.set_comment(_("Added from site"))
            revisions.set_user(self.request.user)
            res = super(AddOrganization, self).form_valid(form)
            self.object.admins.add(self.request.profile)
            self.object.members.add(self.request.profile)
            self.object.save()
            return res


class AddOrganizationContest(
    AdminOrganizationMixin, OrganizationContestMixin, CreateView
):
    template_name = "organization/contest/add.html"
    form_class = AddOrganizationContestForm

    def get_title(self):
        return _("Add contest")

    def get_form_kwargs(self):
        kwargs = super(AddOrganizationContest, self).get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Added from site"))
            revisions.set_user(self.request.user)

            res = super(AddOrganizationContest, self).form_valid(form)

            self.object.organizations.add(self.organization)
            self.object.is_organization_private = True
            self.object.authors.add(self.request.profile)
            self.object.save()
            return res

    def get_success_url(self):
        return reverse(
            "organization_contest_edit",
            args=[self.organization.id, self.organization.slug, self.object.key],
        )


class EditOrganizationContest(
    OrganizationContestMixin, MemberOrganizationMixin, UpdateView
):
    template_name = "organization/contest/edit.html"
    form_class = EditOrganizationContestForm

    def setup_contest(self, request, *args, **kwargs):
        contest_key = kwargs.get("contest", None)
        if not contest_key:
            raise Http404()
        self.contest = get_object_or_404(Contest, key=contest_key)
        if self.organization not in self.contest.organizations.all():
            raise Http404()
        if not self.is_contest_editable(request, self.contest):
            return generic_message(
                self.request,
                _("Permission denied"),
                _("You are not allowed to edit this contest"),
                status=400,
            )

    def get_form_kwargs(self):
        kwargs = super(EditOrganizationContest, self).get_form_kwargs()
        kwargs["org_id"] = self.organization.id
        kwargs["request"] = self.request
        return kwargs

    def get(self, request, *args, **kwargs):
        res = self.setup_contest(request, *args, **kwargs)
        if res:
            return res
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        res = self.setup_contest(request, *args, **kwargs)
        if res:
            return res
        problem_formset = self.get_problem_formset(True)
        quiz_formset = self.get_quiz_formset(True)

        problems_valid = problem_formset.is_valid()
        quizzes_valid = quiz_formset.is_valid()

        if problems_valid and quizzes_valid:
            # Process problem formset
            for problem_form in problem_formset:
                if problem_form.cleaned_data.get("DELETE") and problem_form.instance.pk:
                    problem_form.instance.delete()

            for contest_problem in problem_formset.save(commit=False):
                if contest_problem:
                    contest_problem.contest = self.contest
                    try:
                        contest_problem.save()
                    except (IntegrityError, ValidationError) as e:
                        problem = contest_problem.problem
                        ContestProblem.objects.filter(
                            contest=self.contest, problem=problem
                        ).delete()
                        contest_problem.save()

            # Process quiz formset
            for quiz_form in quiz_formset:
                if quiz_form.cleaned_data.get("DELETE") and quiz_form.instance.pk:
                    quiz_form.instance.delete()

            for contest_quiz in quiz_formset.save(commit=False):
                if contest_quiz:
                    contest_quiz.contest = self.contest
                    try:
                        contest_quiz.save()
                    except (IntegrityError, ValidationError) as e:
                        quiz = contest_quiz.quiz
                        ContestProblem.objects.filter(
                            contest=self.contest, quiz=quiz
                        ).delete()
                        contest_quiz.save()

            return super().post(request, *args, **kwargs)

        self.object = self.contest
        return self.render_to_response(
            self.get_context_data(
                problems_form=problem_formset,
                quizzes_form=quiz_formset,
            )
        )

    def get_title(self):
        return _("Edit %s") % self.contest.key

    def get_content_title(self):
        try:
            href = reverse("contest_view", args=[self.contest.key])
            return mark_safe(_("Edit") + f' <a href="{href}">{self.contest.key}</a>')
        except NoReverseMatch:
            if self.contest.pk:
                original_contest = Contest.objects.get(pk=self.contest.pk)
                href = reverse("contest_view", args=[original_contest.key])
                return mark_safe(
                    _("Edit") + f' <a href="{href}">{original_contest.key}</a>'
                )
            else:
                return _("Edit contest")

    def get_object(self):
        return self.contest

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            res = super(EditOrganizationContest, self).form_valid(form)
            self.object.organizations.add(self.organization)
            self.object.is_organization_private = True
            self.object.save()

            maybe_trigger_contest_rescore(form, self.object, True)

            return res

    def get_problem_formset(self, post=False):
        return ContestProblemFormSet(
            data=self.request.POST if post else None,
            prefix="problems",
            queryset=ContestProblem.objects.filter(
                contest=self.contest, problem__isnull=False
            ).order_by("order"),
        )

    def get_quiz_formset(self, post=False):
        return ContestQuizFormSet(
            data=self.request.POST if post else None,
            prefix="quizzes",
            queryset=ContestProblem.objects.filter(
                contest=self.contest, quiz__isnull=False
            ).order_by("order"),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if "problems_form" not in context:
            context["problems_form"] = self.get_problem_formset()
        if "quizzes_form" not in context:
            context["quizzes_form"] = self.get_quiz_formset()
        return context

    def get_success_url(self):
        # Use the updated contest key in case it was changed
        return reverse(
            "organization_contest_edit",
            args=[self.organization.id, self.organization.slug, self.object.key],
        )


class AddOrganizationBlog(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
    CommunityOrMemberMixin,
    CreateView,
):
    template_name = "organization/blog/add.html"
    model = BlogPost
    form_class = OrganizationBlogForm

    def get_form_class(self):
        if self.can_edit_organization(self.organization):
            return OrganizationAdminBlogForm
        return OrganizationBlogForm

    def get_title(self):
        return _("Add blog for %s") % self.organization.name

    def form_valid(self, form):
        with revisions.create_revision():
            res = super(AddOrganizationBlog, self).form_valid(form)
            self.object.is_organization_private = True
            self.object.authors.add(self.request.profile)
            self.object.slug = slugify(self.object.title)[:50]
            self.object.organizations.add(self.organization)
            self.object.save()

            revisions.set_comment(_("Added from site"))
            revisions.set_user(self.request.user)

            link = reverse(
                "edit_organization_blog",
                args=[self.organization.id, self.organization.slug, self.object.id],
            )
            html = (
                f'<a href="{link}">{self.object.title} - {self.organization.name}</a>'
            )
            Notification.objects.bulk_create_notifications(
                user_ids=self.organization.get_admin_ids()
                + self.organization.get_moderator_ids(),
                category=NotificationCategory.ADD_BLOG,
                html_link=html,
                author=self.request.profile,
            )

            # Add success message for user feedback
            success_message = _(
                "Your blog post has been submitted successfully and is waiting for admin approval."
            )
            if not self.object.visible:
                messages.success(
                    self.request,
                    success_message,
                )

            return res

    def get_success_url(self):
        if not self.object.visible:
            return reverse(
                "organization_pending_blogs",
                args=[self.organization.id, self.organization.slug],
            )
        return reverse(
            "organization_home", args=[self.organization.id, self.organization.slug]
        )


class EditOrganizationBlog(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
    CommunityOrMemberMixin,
    UpdateView,
):
    template_name = "organization/blog/edit.html"
    model = BlogPost

    def get_form_class(self):
        if self.can_edit_organization(
            self.organization
        ) or self.organization.can_moderate(self.request.profile):
            return OrganizationAdminBlogForm
        return OrganizationBlogForm

    def setup_blog(self, request, *args, **kwargs):
        try:
            self.blog_id = kwargs["blog_pk"]
            self.blog = BlogPost.objects.get(id=self.blog_id)
            if self.organization not in self.blog.organizations.all():
                raise Exception(_("This blog does not belong to this group"))

            self.is_org_admin = self.request.profile.can_edit_organization(
                self.organization
            )
            self.is_org_moderator = self.organization.can_moderate(self.request.profile)
            self.is_blog_author = self.request.profile.id in self.blog.get_author_ids()

            if not (self.is_org_admin or self.is_org_moderator or self.is_blog_author):
                raise Exception(_("Not allowed to edit this blog"))

            # Prevent authors from accessing edit page after post is approved (visible=True)
            # Only allow admins and moderators to edit approved posts
            if self.blog.visible and not (self.is_org_admin or self.is_org_moderator):
                raise Exception(_("Cannot edit approved blog posts"))

        except BlogPost.DoesNotExist:
            return generic_message(
                request,
                _("Permission denied"),
                _("Blog post not found"),
                status=404,
            )
        except Exception as e:
            return generic_message(
                request,
                _("Permission denied"),
                str(e),
                status=403,
            )

    def publish_blog(self, request, *args, **kwargs):
        self.blog_id = kwargs["blog_pk"]
        BlogPost.objects.filter(pk=self.blog_id).update(visible=True, is_rejected=False)

    def reject_blog(self, request, *args, **kwargs):
        self.blog_id = kwargs["blog_pk"]
        BlogPost.objects.filter(pk=self.blog_id).update(is_rejected=True)

    def delete_blog(self, request, *args, **kwargs):
        self.blog_id = kwargs["blog_pk"]
        BlogPost.objects.get(pk=self.blog_id).delete()

    def get(self, request, *args, **kwargs):
        res = self.setup_blog(request, *args, **kwargs)
        if res:
            return res
        return super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        res = self.setup_blog(request, *args, **kwargs)
        if res:
            return res
        action = request.POST.get("action")

        if action == "Delete":
            # Only admin or author can delete posts (not moderators)
            if not (self.is_org_admin or self.is_blog_author):
                return generic_message(
                    request,
                    _("Permission denied"),
                    _("You are not allowed to delete this blog."),
                    status=403,
                )
            self.create_notification("Delete blog")
            self.delete_blog(request, *args, **kwargs)
            cur_url = reverse(
                "organization_home",
                args=(self.organization_id, self.organization.slug),
            )
            return HttpResponseRedirect(cur_url)
        elif action == "Reject":
            if not (self.is_org_admin or self.is_org_moderator):
                return generic_message(
                    request,
                    _("Permission denied"),
                    _("Only organization admins and moderators can reject blog posts."),
                    status=403,
                )

            # Log the moderation action (also sends notification)
            note = request.POST.get("note", "")
            OrganizationModerationLog.log_action(
                organization=self.organization,
                content_object=self.blog,
                action="reject_post",
                moderator=request.profile,
                reason=note,
            )
            self.reject_blog(request, *args, **kwargs)
            cur_url = (
                reverse(
                    "organization_pending_blogs",
                    args=(self.organization_id, self.organization.slug),
                )
                + "?tab=rejected"
            )
            return HttpResponseRedirect(cur_url)
        elif action == "Approve":
            if not (self.is_org_admin or self.is_org_moderator):
                return generic_message(
                    request,
                    _("Permission denied"),
                    _(
                        "Only organization admins and moderators can approve blog posts."
                    ),
                    status=403,
                )
            # Log the moderation action (also sends notification)
            note = request.POST.get("note", "")
            OrganizationModerationLog.log_action(
                organization=self.organization,
                content_object=self.blog,
                action="approve_post",
                moderator=request.profile,
                reason=note,
            )
            self.publish_blog(request, *args, **kwargs)
            cur_url = reverse(
                "organization_pending_blogs",
                args=(self.organization_id, self.organization.slug),
            )
            return HttpResponseRedirect(cur_url)
        else:
            return super().post(request, *args, **kwargs)

    def get_object(self):
        return self.blog

    def get_title(self):
        return _("Edit blog %s") % self.object.title

    def create_notification(self, action):
        blog = BlogPost.objects.get(pk=self.blog_id)

        # Use different links based on action - post link for approve/reject, edit link for edit/delete
        if action in ["Approve blog", "Reject blog"]:
            # For approve/reject, link to the actual blog post
            link = blog.get_absolute_url()
        else:
            # For edit/delete, link to the edit page
            link = reverse(
                "edit_organization_blog",
                args=[self.organization.id, self.organization.slug, self.blog_id],
            )

        html = f'<a href="{link}">{blog.title} - {self.organization.name}</a>'
        to_users = list(set(self.organization.get_admin_ids() + blog.get_author_ids()))

        # Use different categories based on action
        if action == "Delete blog":
            category = NotificationCategory.DELETE_BLOG
        elif action == "Reject blog":
            category = NotificationCategory.REJECT_BLOG
        elif action == "Approve blog":
            category = NotificationCategory.APPROVE_BLOG
        else:  # "Edit blog"
            category = NotificationCategory.EDIT_BLOG

        Notification.objects.bulk_create_notifications(
            user_ids=to_users,
            category=category,
            html_link=html,
            author=self.request.profile,
        )

    def form_valid(self, form):
        with revisions.create_revision():
            res = super(EditOrganizationBlog, self).form_valid(form)
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            self.create_notification("Edit blog")

            self.object.slug = slugify(self.object.title)[:50]
            self.object.save()
            return res

    def get_success_url(self):
        if not self.object.visible:
            return reverse(
                "organization_pending_blogs",
                args=[self.organization.id, self.organization.slug],
            )
        return reverse(
            "organization_home", args=[self.organization.id, self.organization.slug]
        )


class PendingBlogs(
    LoginRequiredMixin,
    TitleMixin,
    CommunityOrMemberMixin,
    OrganizationHomeView,
    ListView,
):
    model = BlogPost
    template_name = "organization/blog/pending.html"
    context_object_name = "blogs"

    def get(self, request, *args, **kwargs):
        self.current_tab = request.GET.get("tab", "pending")
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        is_rejected = self.current_tab == "rejected"
        queryset = BlogPost.objects.filter(
            organizations=self.organization,
            visible=False,
            is_rejected=is_rejected,
        )
        # Admins and moderators can see all blogs
        if not (
            self.can_edit_organization(self.organization)
            or self.organization.can_moderate(self.request.profile)
        ):
            queryset = queryset.filter(authors=self.request.profile)
        return queryset.order_by("-publish_on" if is_rejected else "publish_on")

    def get_title(self):
        if self.current_tab == "rejected":
            return _("Rejected blogs in %s") % self.organization.name
        return _("Pending blogs in %s") % self.organization.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["org"] = self.organization
        context["current_tab"] = self.current_tab
        # Count for tab badges
        base_query = BlogPost.objects.filter(
            organizations=self.organization,
            visible=False,
        )
        if not (
            self.can_edit_organization(self.organization)
            or self.organization.can_moderate(self.request.profile)
        ):
            base_query = base_query.filter(authors=self.request.profile)
        context["pending_count"] = base_query.filter(is_rejected=False).count()
        context["rejected_count"] = base_query.filter(is_rejected=True).count()

        # For rejected tab, attach rejection info to each post
        if self.current_tab == "rejected" and context.get("blogs"):
            _attach_rejection_info(context["blogs"], self.organization, BlogPost)

        return context


class OrganizationModerationLogView(
    LoginRequiredMixin,
    TitleMixin,
    OrganizationHomeView,
    ListView,
):
    model = OrganizationModerationLog
    template_name = "organization/moderation_log.html"
    context_object_name = "logs"
    paginate_by = 50

    def dispatch(self, request, *args, **kwargs):
        res = super().dispatch(request, *args, **kwargs)
        if not hasattr(self, "organization"):
            return res
        # Allow admins and moderators
        if self.can_edit_organization(
            self.organization
        ) or self.organization.can_moderate(request.profile):
            return res
        return generic_message(
            request,
            _("Permission denied"),
            _("You are not allowed to view moderation logs."),
            status=403,
        )

    def get_queryset(self):
        return (
            OrganizationModerationLog.objects.filter(organization=self.organization)
            .select_related("moderator", "content_type")
            .order_by("-created_at")
        )

    def get_title(self):
        return _("Moderation Log - %s") % self.organization.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "moderation_log"
        return context


class OrganizationCourses(OrganizationCourseMixin, MemberOrganizationMixin, CourseList):
    template_name = "organization/course_list.html"

    def get(self, request, *args, **kwargs):
        # Initialize kwargs if not present
        self.kwargs = kwargs
        default_tab = "my" if request.user.is_authenticated else "joinable"
        self.current_tab = request.GET.get("tab", default_tab)
        self.search_query = request.GET.get("search", "")
        self.role_filter = request.GET.get("role_filter", "")
        return super(CourseList, self).get(request, *args, **kwargs)

    def get_queryset(self):
        profile = self.request.profile if self.request.user.is_authenticated else None

        # Start with courses in this organization
        queryset = Course.objects.filter(organizations=self.organization)

        if self.current_tab == "my":
            if not profile:
                return Course.objects.none()
            # Filter to user's courses within this organization
            queryset = queryset.filter(courserole__user=profile)

            # Apply role filter only for "my" courses tab
            if self.role_filter:
                if self.role_filter == "teaching":
                    # Filter for Teaching + Assistant roles
                    queryset = queryset.filter(
                        courserole__user=profile,
                        courserole__role__in=[
                            RoleInCourse.TEACHER,
                            RoleInCourse.ASSISTANT,
                        ],
                    )
                elif self.role_filter == "student":
                    # Filter for Student role
                    queryset = queryset.filter(
                        courserole__user=profile, courserole__role=RoleInCourse.STUDENT
                    )
        else:  # Default to "joinable" tab
            # Show joinable courses within this organization
            if profile:
                # Exclude courses user is already in
                user_course_ids = Course.objects.filter(
                    courserole__user=profile
                ).values_list("id", flat=True)
                queryset = queryset.exclude(id__in=user_course_ids)
            # Only show public and open courses for joinable tab
            queryset = queryset.filter(is_public=True, is_open=True)

        if self.search_query:
            queryset = queryset.filter(
                Q(name__icontains=self.search_query)
                | Q(slug__icontains=self.search_query)
            )

        return queryset.order_by("-id").prefetch_related("organizations").distinct()

    def get_context_data(self, **kwargs):
        context = super(OrganizationCourses, self).get_context_data(**kwargs)
        context["title"] = _("Courses in %s") % self.organization.name
        context["page_type"] = "courses"

        # Remove global organizations from context since we're in organization view
        if "organizations" in context:
            context.pop("organizations")

        # Add organization-specific course creation permissions
        context["can_create_course"] = (
            self.request.user.is_superuser
            or self.organization.is_admin(self.request.profile)
        )

        return context
