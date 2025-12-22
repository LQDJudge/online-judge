from django.views.generic import ListView
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.utils.infinite_paginator import InfinitePaginationMixin
from judge.models.profile import (
    OrganizationProfile,
    get_top_rating_profile,
    get_top_score_profile,
    get_rating_rank,
    get_points_rank,
)
from judge.models import ContestProblemClarification, Contest
from judge.utils.users import get_awards


class FeedView(InfinitePaginationMixin, ListView):
    def get_feed_context(self, object_list):
        return {}

    def get(self, request, *args, **kwargs):
        only_content = request.GET.get("only_content", None)
        if only_content and self.feed_content_template_name:
            self.page = int(request.GET.get("page"))
            queryset = self.get_queryset()
            paginator, page, object_list, _ = self.paginate_queryset(
                queryset, self.paginate_by
            )
            context = {
                self.context_object_name: object_list,
                "has_next_page": page.has_next(),
            }
            context.update(self.get_feed_context(object_list))
            return render(request, self.feed_content_template_name, context)

        self.page = 1
        return super(FeedView, self).get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["has_next_page"] = context["page_obj"].has_next()
        try:
            context["feed_content_url"] = reverse(self.url_name)
        except Exception as e:
            context["feed_content_url"] = self.request.path
        return context


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
