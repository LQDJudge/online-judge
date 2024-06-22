import itertools
import json
from datetime import datetime
from operator import itemgetter

from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Permission
from django.contrib.auth.views import redirect_to_login
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Max, Min
from django.db.models.fields import DateField
from django.db.models.functions import Cast, ExtractYear
from judge.models.bookmark import MakeBookMark
from django.forms import Form
from django.http import (
    Http404,
    HttpResponseRedirect,
    JsonResponse,
    HttpResponseForbidden,
    HttpResponseBadRequest,
    HttpResponse,
)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.functional import cached_property
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView
from django.template.loader import render_to_string
from reversion import revisions

from judge.forms import UserForm, ProfileForm, ProfileInfoForm
from judge.models import (
    Profile,
    Rating,
    Submission,
    Friend,
    ProfileInfo,
    BlogPost,
    Problem,
    Contest,
    Solution,
)
from judge.performance_points import get_pp_breakdown
from judge.ratings import rating_class, rating_progress
from judge.tasks import import_users
from judge.utils.problems import contest_completed_ids, user_completed_ids
from judge.utils.ranker import ranker
from judge.utils.unicode import utf8text
from judge.utils.users import (
    get_rating_rank,
    get_points_rank,
    get_awards,
    get_contest_ratings,
)
from judge.utils.views import (
    QueryStringSortMixin,
    TitleMixin,
    generic_message,
    SingleObjectFormView,
    DiggPaginatorMixin,
)
from judge.utils.infinite_paginator import InfinitePaginationMixin
from judge.views.problem import ProblemList
from .contests import ContestRanking


__all__ = [
    "UserPage",
    "UserAboutPage",
    "UserProblemsPage",
    "UserBookMarkPage",
    "users",
    "edit_profile",
]


def remap_keys(iterable, mapping):
    return [dict((mapping.get(k, k), v) for k, v in item.items()) for item in iterable]


class UserMixin(object):
    model = Profile
    slug_field = "user__username"
    slug_url_kwarg = "user"
    context_object_name = "user"

    def render_to_response(self, context, **response_kwargs):
        return super(UserMixin, self).render_to_response(context, **response_kwargs)


class UserPage(TitleMixin, UserMixin, DetailView):
    template_name = "user/user-base.html"

    def get_object(self, queryset=None):
        if self.kwargs.get(self.slug_url_kwarg, None) is None:
            return self.request.profile
        return super(UserPage, self).get_object(queryset)

    def dispatch(self, request, *args, **kwargs):
        if self.kwargs.get(self.slug_url_kwarg, None) is None:
            if not self.request.user.is_authenticated:
                return redirect_to_login(self.request.get_full_path())
        try:
            return super(UserPage, self).dispatch(request, *args, **kwargs)
        except Http404:
            return generic_message(
                request,
                _("No such user"),
                _('No user handle "%s".') % self.kwargs.get(self.slug_url_kwarg, None),
            )

    def get_title(self):
        return (
            _("My account")
            if self.request.profile == self.object
            else _("User %s") % self.object.username
        )

    def get_content_title(self):
        username = self.object.username
        css_class = self.object.css_class
        return mark_safe(f'<span class="{css_class}">{username}</span>')

    # TODO: the same code exists in problem.py, maybe move to problems.py?
    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile

    @cached_property
    def in_contest(self):
        return (
            self.profile is not None
            and self.profile.current_contest is not None
            and self.request.in_contest_mode
        )

    def get_completed_problems(self):
        if self.in_contest:
            return contest_completed_ids(self.profile.current_contest)
        else:
            return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_context_data(self, **kwargs):
        context = super(UserPage, self).get_context_data(**kwargs)

        context["followed"] = Friend.is_friend(self.request.profile, self.object)
        context["hide_solved"] = int(self.hide_solved)
        context["authored"] = self.object.authored_problems.filter(
            is_public=True, is_organization_private=False
        ).order_by("code")

        rating = self.object.ratings.order_by("-contest__end_time")[:1]
        context["rating"] = rating[0] if rating else None

        context["points_rank"] = get_points_rank(self.object)

        if rating:
            context["rating_rank"] = get_rating_rank(self.object)
            context["rated_users"] = Profile.objects.filter(
                is_unlisted=False, rating__isnull=False
            ).count()
        context.update(
            self.object.ratings.aggregate(
                min_rating=Min("rating"),
                max_rating=Max("rating"),
                contests=Count("contest"),
            )
        )
        return context

    def get(self, request, *args, **kwargs):
        self.hide_solved = (
            request.GET.get("hide_solved") == "1"
            if "hide_solved" in request.GET
            else False
        )
        return super(UserPage, self).get(request, *args, **kwargs)


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class UserAboutPage(UserPage):
    template_name = "user/user-about.html"

    def get_context_data(self, **kwargs):
        context = super(UserAboutPage, self).get_context_data(**kwargs)
        ratings = context["ratings"] = get_contest_ratings(self.object)

        context["rating_data"] = mark_safe(
            json.dumps(
                [
                    {
                        "label": rating.contest.name,
                        "rating": rating.rating,
                        "ranking": rating.rank,
                        "link": reverse("contest_ranking", args=(rating.contest.key,))
                        + "#!"
                        + self.object.username,
                        "timestamp": (rating.contest.end_time - EPOCH).total_seconds()
                        * 1000,
                        "date": date_format(
                            timezone.localtime(rating.contest.end_time),
                            _("M j, Y, G:i"),
                        ),
                        "class": rating_class(rating.rating),
                        "height": "%.3fem" % rating_progress(rating.rating),
                    }
                    for rating in ratings
                ]
            )
        )

        context["awards"] = get_awards(self.object)

        if ratings:
            user_data = self.object.ratings.aggregate(Min("rating"), Max("rating"))
            global_data = Rating.objects.aggregate(Min("rating"), Max("rating"))
            min_ever, max_ever = global_data["rating__min"], global_data["rating__max"]
            min_user, max_user = user_data["rating__min"], user_data["rating__max"]
            delta = max_user - min_user
            ratio = (
                (max_ever - max_user) / (max_ever - min_ever)
                if max_ever != min_ever
                else 1.0
            )
            context["max_graph"] = max_user + ratio * delta
            context["min_graph"] = min_user + ratio * delta - delta

        submissions = (
            self.object.submission_set.annotate(date_only=Cast("date", DateField()))
            .values("date_only")
            .annotate(cnt=Count("id"))
        )

        context["submission_data"] = mark_safe(
            json.dumps(
                {
                    date_counts["date_only"].isoformat(): date_counts["cnt"]
                    for date_counts in submissions
                }
            )
        )
        context["submission_metadata"] = mark_safe(
            json.dumps(
                {
                    "min_year": (
                        self.object.submission_set.annotate(
                            year_only=ExtractYear("date")
                        ).aggregate(min_year=Min("year_only"))["min_year"]
                    ),
                }
            )
        )

        return context


class UserProblemsPage(UserPage):
    template_name = "user/user-problems.html"

    def get_context_data(self, **kwargs):
        context = super(UserProblemsPage, self).get_context_data(**kwargs)

        result = (
            Submission.objects.filter(
                user=self.object,
                points__gt=0,
                problem__is_public=True,
                problem__is_organization_private=False,
            )
            .exclude(
                problem__in=self.get_completed_problems() if self.hide_solved else []
            )
            .values(
                "problem__id",
                "problem__code",
                "problem__name",
                "problem__points",
                "problem__group__full_name",
            )
            .distinct()
            .annotate(points=Max("points"))
            .order_by("problem__group__full_name", "problem__code")
        )

        def process_group(group, problems_iter):
            problems = list(problems_iter)
            points = sum(map(itemgetter("points"), problems))
            return {"name": group, "problems": problems, "points": points}

        context["best_submissions"] = [
            process_group(group, problems)
            for group, problems in itertools.groupby(
                remap_keys(
                    result,
                    {
                        "problem__code": "code",
                        "problem__name": "name",
                        "problem__points": "total",
                        "problem__group__full_name": "group",
                    },
                ),
                itemgetter("group"),
            )
        ]
        breakdown, has_more = get_pp_breakdown(self.object, start=0, end=10)
        context["pp_breakdown"] = breakdown
        context["pp_has_more"] = has_more

        return context


class UserBookMarkPage(DiggPaginatorMixin, ListView, UserPage):
    template_name = "user/user-bookmarks.html"
    context_object_name = "bookmarks"
    paginate_by = 10

    def get(self, request, *args, **kwargs):
        self.current_tab = self.request.GET.get("tab", "problems")
        self.user = self.object = self.get_object()
        return super(UserBookMarkPage, self).get(request, *args, **kwargs)

    def get_queryset(self):
        model = None
        if self.current_tab == "posts":
            model = BlogPost
        elif self.current_tab == "contests":
            model = Contest
        elif self.current_tab == "editorials":
            model = Solution
        else:
            model = Problem

        q = MakeBookMark.objects.filter(user=self.user).select_related("bookmark")
        q = q.filter(bookmark__content_type=ContentType.objects.get_for_model(model))
        object_ids = q.values_list("bookmark__object_id", flat=True)

        res = model.objects.filter(id__in=object_ids)
        if self.current_tab == "contests":
            res = res.prefetch_related("organizations", "tags")
        elif self.current_tab == "editorials":
            res = res.select_related("problem")

        return res

    def get_context_data(self, **kwargs):
        context = super(UserBookMarkPage, self).get_context_data(**kwargs)

        context["current_tab"] = self.current_tab
        context["user"] = self.user

        context["page_prefix"] = (
            self.request.path + "?tab=" + self.current_tab + "&page="
        )
        context["first_page_href"] = self.request.path

        return context


class UserPerformancePointsAjax(UserProblemsPage):
    template_name = "user/pp-table-body.html"

    def get_context_data(self, **kwargs):
        context = super(UserPerformancePointsAjax, self).get_context_data(**kwargs)
        try:
            start = int(self.request.GET.get("start", 0))
            end = int(self.request.GET.get("end", settings.DMOJ_PP_ENTRIES))
            if start < 0 or end < 0 or start > end:
                raise ValueError
        except ValueError:
            start, end = 0, 100
        breakdown, self.has_more = get_pp_breakdown(self.object, start=start, end=end)
        context["pp_breakdown"] = breakdown
        return context

    def get(self, request, *args, **kwargs):
        httpresp = super(UserPerformancePointsAjax, self).get(request, *args, **kwargs)
        httpresp.render()

        return JsonResponse(
            {
                "results": utf8text(httpresp.content),
                "has_more": self.has_more,
            }
        )


@login_required
def edit_profile(request):
    profile = request.profile
    profile_info, created = ProfileInfo.objects.get_or_create(profile=profile)
    if request.method == "POST":
        form_user = UserForm(request.POST, instance=request.user)
        form = ProfileForm(
            request.POST, request.FILES, instance=profile, user=request.user
        )
        form_info = ProfileInfoForm(request.POST, instance=profile_info)
        if form_user.is_valid() and form.is_valid():
            with revisions.create_revision():
                form_user.save()
                form.save()
                form_info.save()
                revisions.set_user(request.user)
                revisions.set_comment(_("Updated on site"))
            return HttpResponseRedirect(request.path)
    else:
        form_user = UserForm(instance=request.user)
        form = ProfileForm(instance=profile, user=request.user)
        form_info = ProfileInfoForm(instance=profile_info)

    tzmap = settings.TIMEZONE_MAP

    return render(
        request,
        "user/edit-profile.html",
        {
            "require_staff_2fa": settings.DMOJ_REQUIRE_STAFF_2FA,
            "form_user": form_user,
            "form": form,
            "form_info": form_info,
            "title": _("Edit profile"),
            "profile": profile,
            "TIMEZONE_MAP": tzmap or "http://momentjs.com/static/img/world.png",
            "TIMEZONE_BG": settings.TIMEZONE_BG if tzmap else "#4E7CAD",
        },
    )


class UserList(QueryStringSortMixin, InfinitePaginationMixin, TitleMixin, ListView):
    model = Profile
    title = gettext_lazy("Leaderboard")
    context_object_name = "users"
    template_name = "user/list.html"
    paginate_by = 100
    all_sorts = frozenset(("points", "problem_count", "rating", "performance_points"))
    default_desc = all_sorts
    default_sort = "-performance_points"
    filter_friend = False

    def filter_friend_queryset(self, queryset):
        friends = self.request.profile.get_friends()
        ret = queryset.filter(id__in=friends)
        return ret

    def get_queryset(self):
        queryset = (
            Profile.objects.filter(is_unlisted=False)
            .order_by(self.order, "id")
            .only(
                "display_rank",
                "points",
                "rating",
                "performance_points",
                "problem_count",
                "about",
            )
        )
        if self.request.organization:
            queryset = queryset.filter(organizations=self.request.organization)
        if (self.request.GET.get("friend") == "true") and self.request.profile:
            queryset = self.filter_friend_queryset(queryset)
            self.filter_friend = True
        return queryset

    def get_context_data(self, **kwargs):
        context = super(UserList, self).get_context_data(**kwargs)
        Profile.prefetch_profile_cache([u.id for u in context["users"]])
        context["users"] = ranker(
            context["users"], rank=self.paginate_by * (context["page_obj"].number - 1)
        )
        context["first_page_href"] = "."
        context["page_type"] = "friends" if self.filter_friend else "list"
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context


user_list_view = UserList.as_view()


class FixedContestRanking(ContestRanking):
    contest = None

    def get_object(self, queryset=None):
        return self.contest


def users(request):
    if request.user.is_authenticated:
        if request.in_contest_mode:
            participation = request.profile.current_contest
            contest = participation.contest
            return FixedContestRanking.as_view(contest=contest)(
                request, contest=contest.key
            )
    return user_list_view(request)


def user_ranking_redirect(request):
    try:
        username = request.GET["handle"]
    except KeyError:
        raise Http404()
    user = get_object_or_404(Profile, user__username=username)
    rank = Profile.objects.filter(
        is_unlisted=False, performance_points__gt=user.performance_points
    ).count()
    rank += Profile.objects.filter(
        is_unlisted=False,
        performance_points__exact=user.performance_points,
        id__lt=user.id,
    ).count()
    page = rank // UserList.paginate_by
    return HttpResponseRedirect(
        "%s%s#!%s"
        % (reverse("user_list"), "?page=%d" % (page + 1) if page else "", username)
    )


class UserLogoutView(TitleMixin, TemplateView):
    template_name = "registration/logout.html"
    title = "You have been successfully logged out."

    def post(self, request, *args, **kwargs):
        auth_logout(request)
        return HttpResponseRedirect(request.get_full_path())


class ImportUsersView(TitleMixin, TemplateView):
    template_name = "user/import/index.html"
    title = _("Import Users")

    def get(self, *args, **kwargs):
        if self.request.user.is_superuser:
            return super().get(self, *args, **kwargs)
        return HttpResponseForbidden()


def import_users_post_file(request):
    if not request.user.is_superuser or request.method != "POST":
        return HttpResponseForbidden()
    users = import_users.csv_to_dict(request.FILES["csv_file"])

    if not users:
        return JsonResponse(
            {
                "done": False,
                "msg": "No valid row found. Make sure row containing username.",
            }
        )

    table_html = render_to_string("user/import/table_csv.html", {"data": users})
    return JsonResponse({"done": True, "html": table_html, "data": users})


def import_users_submit(request):
    import json

    if not request.user.is_superuser or request.method != "POST":
        return HttpResponseForbidden()

    users = json.loads(request.body)["users"]
    log = import_users.import_users(users)
    return JsonResponse({"msg": log})


def sample_import_users(request):
    if not request.user.is_superuser or request.method != "GET":
        return HttpResponseForbidden()
    filename = "import_sample.csv"
    content = ",".join(import_users.fields) + "\n" + ",".join(import_users.descriptions)
    response = HttpResponse(content, content_type="text/plain")
    response["Content-Disposition"] = "attachment; filename={0}".format(filename)
    return response


def toggle_darkmode(request):
    path = request.GET.get("next")
    if not path:
        return HttpResponseBadRequest()
    request.session["darkmode"] = not request.session.get("darkmode", False)
    return HttpResponseRedirect(path)


@login_required
def toggle_follow(request, user):
    if request.method != "POST":
        raise Http404()

    profile_to_follow = get_object_or_404(Profile, user__username=user)

    if request.profile.id == profile_to_follow.id:
        raise Http404()

    Friend.toggle_friend(request.profile, profile_to_follow)
    return HttpResponseRedirect(reverse("user_page", args=(user,)))
