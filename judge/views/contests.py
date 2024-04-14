from copy import deepcopy
import json
import math
from calendar import Calendar, SUNDAY
from collections import defaultdict, namedtuple
from datetime import date, datetime, time, timedelta
from functools import partial
from itertools import chain
from operator import attrgetter, itemgetter

from django import forms
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db import IntegrityError
from django.db.models import (
    Case,
    Count,
    F,
    FloatField,
    IntegerField,
    Max,
    Min,
    Q,
    Sum,
    Value,
    When,
)
from django.dispatch import receiver
from django.db.models.expressions import CombinedExpression
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
    JsonResponse,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import date as date_filter
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import format_html, escape
from django.utils.safestring import mark_safe
from django.utils.timezone import make_aware
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import ListView, TemplateView
from django.views.generic.detail import (
    BaseDetailView,
    DetailView,
    SingleObjectMixin,
    View,
)

from judge import event_poster as event
from judge.views.comment import CommentedDetailView
from judge.forms import ContestCloneForm
from judge.models import (
    Contest,
    ContestMoss,
    ContestParticipation,
    ContestProblem,
    ContestTag,
    Organization,
    Problem,
    Profile,
    Submission,
    ContestProblemClarification,
    ContestsSummary,
)
from judge.tasks import run_moss
from judge.utils.celery import redirect_to_task_status
from judge.utils.opengraph import generate_opengraph
from judge.utils.problems import _get_result_data
from judge.utils.ranker import ranker
from judge.utils.stats import get_bar_chart, get_pie_chart, get_histogram
from judge.utils.views import (
    DiggPaginatorMixin,
    QueryStringSortMixin,
    SingleObjectFormView,
    TitleMixin,
    generic_message,
)
from judge.widgets import HeavyPreviewPageDownWidget
from judge.views.pagevote import PageVoteDetailView
from judge.views.bookmark import BookMarkDetailView


__all__ = [
    "ContestList",
    "ContestDetail",
    "ContestRanking",
    "ContestJoin",
    "ContestLeave",
    "ContestCalendar",
    "ContestClone",
    "ContestStats",
    "ContestMossView",
    "ContestMossDelete",
    "contest_ranking_ajax",
    "ContestParticipationList",
    "ContestParticipationDisqualify",
    "get_contest_ranking_list",
    "base_contest_ranking_list",
    "ContestClarificationView",
    "update_contest_mode",
]


def _find_contest(request, key):
    try:
        contest = Contest.objects.get(key=key)
        private_check = not contest.public_scoreboard
        if private_check and not contest.is_accessible_by(request.user):
            raise ObjectDoesNotExist()
    except ObjectDoesNotExist:
        return (
            generic_message(
                request,
                _("No such contest"),
                _('Could not find a contest with the key "%s".') % key,
                status=404,
            ),
            False,
        )
    return contest, True


class ContestListMixin(object):
    def get_queryset(self):
        return Contest.get_visible_contests(self.request.user)


class ContestList(
    QueryStringSortMixin, DiggPaginatorMixin, TitleMixin, ContestListMixin, ListView
):
    model = Contest
    paginate_by = 10
    template_name = "contest/list.html"
    title = gettext_lazy("Contests")
    context_object_name = "past_contests"
    all_sorts = frozenset(("name", "user_count", "start_time"))
    default_desc = frozenset(("name", "user_count"))

    def get_default_sort_order(self, request):
        if request.GET.get("contest") and settings.ENABLE_FTS:
            return "-relevance"
        return "-start_time"

    @cached_property
    def _now(self):
        return timezone.now()

    def GET_with_session(self, request, key):
        if not request.GET.get(key):
            return request.session.get(key, False)
        return request.GET.get(key, None) == "1"

    def update_session(self, request):
        to_update = ("show_orgs",)
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == "1"
                request.session[key] = val
            else:
                request.session[key] = False

    def get(self, request, *args, **kwargs):
        self.contest_query = None
        self.org_query = []
        self.show_orgs = 0
        if self.GET_with_session(request, "show_orgs"):
            self.show_orgs = 1

        if self.request.GET.get("orgs") and self.request.profile:
            try:
                self.org_query = list(map(int, request.GET.getlist("orgs")))
                if not self.request.user.is_superuser:
                    self.org_query = [
                        i
                        for i in self.org_query
                        if i
                        in set(
                            self.request.profile.organizations.values_list(
                                "id", flat=True
                            )
                        )
                    ]
            except ValueError:
                pass

        self.update_session(request)
        return super(ContestList, self).get(request, *args, **kwargs)

    def _get_queryset(self):
        queryset = (
            super(ContestList, self)
            .get_queryset()
            .prefetch_related("tags", "organizations", "authors", "curators", "testers")
        )

        if self.request.GET.get("contest"):
            self.contest_query = query = " ".join(
                self.request.GET.getlist("contest")
            ).strip()
            if query:
                substr_queryset = queryset.filter(
                    Q(key__icontains=query) | Q(name__icontains=query)
                )
                if settings.ENABLE_FTS:
                    queryset = (
                        queryset.search(query).extra(order_by=["-relevance"])
                        | substr_queryset
                    )
                else:
                    queryset = substr_queryset
        if not self.org_query and self.request.organization:
            self.org_query = [self.request.organization.id]
        if self.show_orgs:
            queryset = queryset.filter(organizations=None)
        if self.org_query:
            queryset = queryset.filter(organizations__in=self.org_query)

        return queryset

    def get_queryset(self):
        return (
            self._get_queryset()
            .order_by(self.order, "key")
            .filter(end_time__lt=self._now)
        )

    def get_context_data(self, **kwargs):
        context = super(ContestList, self).get_context_data(**kwargs)
        present, active, future = [], [], []
        for contest in self._get_queryset().exclude(end_time__lt=self._now):
            if contest.start_time > self._now:
                future.append(contest)
            else:
                present.append(contest)

        if self.request.user.is_authenticated:
            for participation in (
                ContestParticipation.objects.filter(
                    virtual=0, user=self.request.profile, contest_id__in=present
                )
                .select_related("contest")
                .prefetch_related(
                    "contest__authors", "contest__curators", "contest__testers"
                )
                .annotate(key=F("contest__key"))
            ):
                if not participation.ended:
                    active.append(participation)
                    present.remove(participation.contest)

        if not ("contest" in self.request.GET and settings.ENABLE_FTS):
            active.sort(key=attrgetter("end_time", "key"))
            present.sort(key=attrgetter("end_time", "key"))
            future.sort(key=attrgetter("start_time"))
        context["active_participations"] = active
        context["current_contests"] = present
        context["future_contests"] = future
        context["now"] = self._now
        context["first_page_href"] = "."
        context["contest_query"] = self.contest_query
        context["org_query"] = self.org_query
        context["show_orgs"] = int(self.show_orgs)
        if self.request.profile:
            context["organizations"] = self.request.profile.organizations.all()
        context["page_type"] = "list"
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context


class PrivateContestError(Exception):
    def __init__(self, name, is_private, is_organization_private, orgs):
        self.name = name
        self.is_private = is_private
        self.is_organization_private = is_organization_private
        self.orgs = orgs


class ContestMixin(object):
    context_object_name = "contest"
    model = Contest
    slug_field = "key"
    slug_url_kwarg = "contest"

    @cached_property
    def is_editor(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.editor_ids

    @cached_property
    def is_tester(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.tester_ids

    @cached_property
    def can_edit(self):
        return self.object.is_editable_by(self.request.user)

    @cached_property
    def can_access(self):
        return self.object.is_accessible_by(self.request.user)

    def should_bypass_access_check(self, contest):
        return False

    def get_context_data(self, **kwargs):
        context = super(ContestMixin, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            try:
                context[
                    "live_participation"
                ] = self.request.profile.contest_history.get(
                    contest=self.object,
                    virtual=ContestParticipation.LIVE,
                )
            except ContestParticipation.DoesNotExist:
                context["live_participation"] = None
                context["has_joined"] = False
            else:
                context["has_joined"] = True
        else:
            context["live_participation"] = None
            context["has_joined"] = False

        context["now"] = timezone.now()
        context["is_editor"] = self.is_editor
        context["is_tester"] = self.is_tester
        context["can_edit"] = self.can_edit
        context["can_access"] = self.can_access

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph(
                "generated-meta-contest:%d" % self.object.id,
                self.object.description,
            )
        context["meta_description"] = self.object.summary or metadata[0]
        context["og_image"] = self.object.og_image or metadata[1]
        context["has_moss_api_key"] = settings.MOSS_API_KEY is not None
        context["contest_has_hidden_subtasks"] = self.object.format.has_hidden_subtasks
        context[
            "show_final_ranking"
        ] = self.object.format.has_hidden_subtasks and self.object.is_editable_by(
            self.request.user
        )
        context["logo_override_image"] = self.object.logo_override_image
        if (
            not context["logo_override_image"]
            and self.object.organizations.count() == 1
        ):
            context[
                "logo_override_image"
            ] = self.object.organizations.first().logo_override_image

        return context

    def get_object(self, queryset=None):
        contest = super(ContestMixin, self).get_object(queryset)
        profile = self.request.profile

        if (
            profile is not None
            and ContestParticipation.objects.filter(
                id=profile.current_contest_id, contest_id=contest.id
            ).exists()
        ):
            return contest

        if self.should_bypass_access_check(contest):
            return contest

        try:
            contest.access_check(self.request.user)
        except Contest.PrivateContest:
            raise PrivateContestError(
                contest.name,
                contest.is_private,
                contest.is_organization_private,
                contest.organizations.all(),
            )
        except Contest.Inaccessible:
            raise Http404()
        else:
            return contest

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(ContestMixin, self).dispatch(request, *args, **kwargs)
        except Http404:
            key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(
                    request,
                    _("No such contest"),
                    _('Could not find a contest with the key "%s".') % key,
                )
            else:
                return generic_message(
                    request, _("No such contest"), _("Could not find such contest.")
                )
        except PrivateContestError as e:
            return render(
                request,
                "contest/private.html",
                {
                    "error": e,
                    "title": _('Access to contest "%s" denied') % e.name,
                },
                status=403,
            )


class ContestDetail(
    ContestMixin,
    TitleMixin,
    CommentedDetailView,
    PageVoteDetailView,
    BookMarkDetailView,
):
    template_name = "contest/contest.html"

    def get_title(self):
        return self.object.name

    def get_editable_organizations(self):
        if not self.request.profile:
            return []
        res = []
        for organization in self.object.organizations.all():
            can_edit = False
            if self.request.profile.can_edit_organization(organization):
                can_edit = True
            if self.request.profile in organization and self.object.is_editable_by(
                self.request.user
            ):
                can_edit = True
            if can_edit:
                res.append(organization)
        return res

    def get_context_data(self, **kwargs):
        context = super(ContestDetail, self).get_context_data(**kwargs)
        context["contest_problems"] = (
            Problem.objects.filter(contests__contest=self.object)
            .order_by("contests__order")
            .defer("description")
            .annotate(
                has_public_editorial=Sum(
                    Case(
                        When(solution__is_public=True, then=1),
                        default=0,
                        output_field=IntegerField(),
                    )
                )
            )
            .add_i18n_name(self.request.LANGUAGE_CODE)
        )
        context["editable_organizations"] = self.get_editable_organizations()
        context["is_clonable"] = is_contest_clonable(self.request, self.object)
        return context


def is_contest_clonable(request, contest):
    if not request.profile:
        return False
    if not Organization.objects.filter(admins=request.profile).exists():
        return False
    if request.user.has_perm("judge.clone_contest"):
        return True
    if contest.access_code and not contest.is_editable_by(request.user):
        return False
    if contest.ended:
        return True
    return False


class ContestClone(ContestMixin, TitleMixin, SingleObjectFormView):
    title = _("Clone Contest")
    template_name = "contest/clone.html"
    form_class = ContestCloneForm

    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if not is_contest_clonable(self.request, contest):
            raise Http404()
        return contest

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["org_choices"] = tuple(
            Organization.objects.filter(admins=self.request.profile).values_list(
                "id", "name"
            )
        )
        kwargs["profile"] = self.request.profile
        return kwargs

    def form_valid(self, form):
        tags = self.object.tags.all()
        organization = form.cleaned_data["organization"]
        private_contestants = self.object.private_contestants.all()
        view_contest_scoreboard = self.object.view_contest_scoreboard.all()
        contest_problems = self.object.contest_problems.all()

        contest = deepcopy(self.object)

        contest.pk = None
        contest.is_visible = False
        contest.user_count = 0
        contest.key = form.cleaned_data["key"]
        contest.is_rated = False
        contest.save()

        contest.tags.set(tags)
        contest.organizations.set([organization])
        contest.private_contestants.set(private_contestants)
        contest.view_contest_scoreboard.set(view_contest_scoreboard)
        contest.authors.add(self.request.profile)

        for problem in contest_problems:
            problem.contest = contest
            problem.pk = None
        ContestProblem.objects.bulk_create(contest_problems)

        return HttpResponseRedirect(
            reverse(
                "organization_contest_edit",
                args=(
                    organization.id,
                    organization.slug,
                    contest.key,
                ),
            )
        )


class ContestAccessDenied(Exception):
    pass


class ContestAccessCodeForm(forms.Form):
    access_code = forms.CharField(max_length=255)

    def __init__(self, *args, **kwargs):
        super(ContestAccessCodeForm, self).__init__(*args, **kwargs)
        self.fields["access_code"].widget.attrs.update({"autocomplete": "off"})


class ContestJoin(LoginRequiredMixin, ContestMixin, BaseDetailView):
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.ask_for_access_code()

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            return self.join_contest(request)
        except ContestAccessDenied:
            if request.POST.get("access_code"):
                return self.ask_for_access_code(ContestAccessCodeForm(request.POST))
            else:
                return HttpResponseRedirect(request.path)

    def join_contest(self, request, access_code=None):
        contest = self.object

        if not contest.can_join and not (self.is_editor or self.is_tester):
            return generic_message(
                request,
                _("Contest not ongoing"),
                _('"%s" is not currently ongoing.') % contest.name,
            )

        profile = request.profile
        if profile.current_contest is not None:
            return generic_message(
                request,
                _("Already in contest"),
                _('You are already in a contest: "%s".')
                % profile.current_contest.contest.name,
            )

        if (
            not request.user.is_superuser
            and contest.banned_users.filter(id=profile.id).exists()
        ):
            return generic_message(
                request,
                _("Banned from joining"),
                _(
                    "You have been declared persona non grata for this contest. "
                    "You are permanently barred from joining this contest."
                ),
            )

        requires_access_code = (
            not self.can_edit
            and contest.access_code
            and access_code != contest.access_code
        )
        if contest.ended:
            if requires_access_code:
                raise ContestAccessDenied()

            while True:
                virtual_id = max(
                    (
                        ContestParticipation.objects.filter(
                            contest=contest, user=profile
                        ).aggregate(virtual_id=Max("virtual"))["virtual_id"]
                        or 0
                    )
                    + 1,
                    1,
                )
                try:
                    participation = ContestParticipation.objects.create(
                        contest=contest,
                        user=profile,
                        virtual=virtual_id,
                        real_start=timezone.now(),
                    )
                # There is obviously a race condition here, so we keep trying until we win the race.
                except IntegrityError:
                    pass
                else:
                    break
        else:
            SPECTATE = ContestParticipation.SPECTATE
            LIVE = ContestParticipation.LIVE
            try:
                participation = ContestParticipation.objects.get(
                    contest=contest,
                    user=profile,
                    virtual=(SPECTATE if self.is_editor or self.is_tester else LIVE),
                )
            except ContestParticipation.DoesNotExist:
                if requires_access_code:
                    raise ContestAccessDenied()

                participation = ContestParticipation.objects.create(
                    contest=contest,
                    user=profile,
                    virtual=(SPECTATE if self.is_editor or self.is_tester else LIVE),
                    real_start=timezone.now(),
                )
            else:
                if participation.ended:
                    participation = ContestParticipation.objects.get_or_create(
                        contest=contest,
                        user=profile,
                        virtual=SPECTATE,
                        defaults={"real_start": timezone.now()},
                    )[0]

        profile.current_contest = participation
        profile.save()
        contest._updating_stats_only = True
        contest.update_user_count()
        return HttpResponseRedirect(reverse("problem_list"))

    def ask_for_access_code(self, form=None):
        contest = self.object
        wrong_code = False
        if form:
            if form.is_valid():
                if form.cleaned_data["access_code"] == contest.access_code:
                    return self.join_contest(
                        self.request, form.cleaned_data["access_code"]
                    )
                wrong_code = True
        else:
            form = ContestAccessCodeForm()
        return render(
            self.request,
            "contest/access_code.html",
            {
                "form": form,
                "wrong_code": wrong_code,
                "title": _('Enter access code for "%s"') % contest.name,
            },
        )


class ContestLeave(LoginRequiredMixin, ContestMixin, BaseDetailView):
    def post(self, request, *args, **kwargs):
        contest = self.get_object()

        profile = request.profile
        if (
            profile.current_contest is None
            or profile.current_contest.contest_id != contest.id
        ):
            return generic_message(
                request,
                _("No such contest"),
                _('You are not in contest "%s".') % contest.key,
                404,
            )

        profile.remove_contest()
        request.session["contest_mode"] = True  # reset contest_mode
        return HttpResponseRedirect(reverse("contest_view", args=(contest.key,)))


ContestDay = namedtuple("ContestDay", "date weekday is_pad is_today starts ends oneday")


class ContestCalendar(TitleMixin, ContestListMixin, TemplateView):
    firstweekday = SUNDAY
    weekday_classes = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
    template_name = "contest/calendar.html"

    def get(self, request, *args, **kwargs):
        try:
            self.year = int(kwargs["year"])
            self.month = int(kwargs["month"])
        except (KeyError, ValueError):
            raise ImproperlyConfigured(
                _("ContestCalendar requires integer year and month")
            )
        self.today = timezone.now().date()
        return self.render()

    def render(self):
        context = self.get_context_data()
        return self.render_to_response(context)

    def get_contest_data(self, start, end):
        end += timedelta(days=1)
        contests = self.get_queryset().filter(
            Q(start_time__gte=start, start_time__lt=end)
            | Q(end_time__gte=start, end_time__lt=end)
        )
        starts, ends, oneday = (defaultdict(list) for i in range(3))
        for contest in contests:
            start_date = timezone.localtime(contest.start_time).date()
            end_date = timezone.localtime(
                contest.end_time - timedelta(seconds=1)
            ).date()
            if start_date == end_date:
                oneday[start_date].append(contest)
            else:
                starts[start_date].append(contest)
                ends[end_date].append(contest)
        return starts, ends, oneday

    def get_table(self):
        calendar = Calendar(self.firstweekday).monthdatescalendar(self.year, self.month)
        starts, ends, oneday = self.get_contest_data(
            make_aware(datetime.combine(calendar[0][0], time.min)),
            make_aware(datetime.combine(calendar[-1][-1], time.min)),
        )
        return [
            [
                ContestDay(
                    date=date,
                    weekday=self.weekday_classes[weekday],
                    is_pad=date.month != self.month,
                    is_today=date == self.today,
                    starts=starts[date],
                    ends=ends[date],
                    oneday=oneday[date],
                )
                for weekday, date in enumerate(week)
            ]
            for week in calendar
        ]

    def get_context_data(self, **kwargs):
        context = super(ContestCalendar, self).get_context_data(**kwargs)

        try:
            month = date(self.year, self.month, 1)
        except ValueError:
            raise Http404()
        else:
            context["title"] = _("Contests in %(month)s") % {
                "month": date_filter(month, _("F Y"))
            }

        dates = Contest.objects.aggregate(min=Min("start_time"), max=Max("end_time"))
        min_month = (self.today.year, self.today.month)
        if dates["min"] is not None:
            min_month = dates["min"].year, dates["min"].month
        max_month = (self.today.year, self.today.month)
        if dates["max"] is not None:
            max_month = max(
                (dates["max"].year, dates["max"].month),
                (self.today.year, self.today.month),
            )

        month = (self.year, self.month)
        if month < min_month or month > max_month:
            # 404 is valid because it merely declares the lack of existence, without any reason
            raise Http404()

        context["now"] = timezone.now()
        context["calendar"] = self.get_table()
        context["curr_month"] = date(self.year, self.month, 1)

        if month > min_month:
            context["prev_month"] = date(
                self.year - (self.month == 1),
                12 if self.month == 1 else self.month - 1,
                1,
            )
        else:
            context["prev_month"] = None

        if month < max_month:
            context["next_month"] = date(
                self.year + (self.month == 12),
                1 if self.month == 12 else self.month + 1,
                1,
            )
        else:
            context["next_month"] = None
        return context


class CachedContestCalendar(ContestCalendar):
    def render(self):
        key = "contest_cal:%d:%d" % (self.year, self.month)
        cached = cache.get(key)
        if cached is not None:
            return HttpResponse(cached)
        response = super(CachedContestCalendar, self).render()
        response.render()
        cached.set(key, response.content)
        return response


class ContestStats(TitleMixin, ContestMixin, DetailView):
    template_name = "contest/stats.html"
    POINT_BIN = 10  # in point distribution

    def get_title(self):
        return _("%s Statistics") % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not (self.object.ended or self.can_edit):
            raise Http404()

        queryset = Submission.objects.filter(contest_object=self.object)

        ac_count = Count(
            Case(When(result="AC", then=Value(1)), output_field=IntegerField())
        )
        ac_rate = CombinedExpression(
            ac_count / Count("problem"), "*", Value(100.0), output_field=FloatField()
        )

        status_count_queryset = list(
            queryset.values("problem__code", "result")
            .annotate(count=Count("result"))
            .values_list("problem__code", "result", "count"),
        )
        labels, codes = [], []
        contest_problems = self.object.contest_problems.order_by("order").values_list(
            "problem__name", "problem__code"
        )
        if contest_problems:
            labels, codes = zip(*contest_problems)
        num_problems = len(labels)
        status_counts = [[] for i in range(num_problems)]
        for problem_code, result, count in status_count_queryset:
            if problem_code in codes:
                status_counts[codes.index(problem_code)].append((result, count))

        result_data = defaultdict(partial(list, [0] * num_problems))
        for i in range(num_problems):
            for category in _get_result_data(defaultdict(int, status_counts[i]))[
                "categories"
            ]:
                result_data[category["code"]][i] = category["count"]

        problem_points = [[] for _ in range(num_problems)]
        point_count_queryset = list(
            queryset.values(
                "problem__code", "contest__points", "contest__problem__points"
            )
            .annotate(count=Count("contest__points"))
            .order_by("problem__code", "contest__points")
            .values_list(
                "problem__code", "contest__points", "contest__problem__points", "count"
            )
        )
        counter = [[0 for _ in range(self.POINT_BIN + 1)] for _ in range(num_problems)]
        for problem_code, point, max_point, count in point_count_queryset:
            if (point == None) or (problem_code not in codes):
                continue
            problem_idx = codes.index(problem_code)
            bin_idx = math.floor(point * self.POINT_BIN / max_point)
            bin_idx = max(min(bin_idx, self.POINT_BIN), 0)
            counter[problem_idx][bin_idx] += count
        for i in range(num_problems):
            problem_points[i] = [
                (j * 100 / self.POINT_BIN, counter[i][j])
                for j in range(len(counter[i]))
            ]

        stats = {
            "problem_status_count": {
                "labels": labels,
                "datasets": [
                    {
                        "label": name,
                        "backgroundColor": settings.DMOJ_STATS_SUBMISSION_RESULT_COLORS[
                            name
                        ],
                        "data": data,
                    }
                    for name, data in result_data.items()
                ],
            },
            "problem_ac_rate": get_bar_chart(
                queryset.values("contest__problem__order", "problem__name")
                .annotate(ac_rate=ac_rate)
                .order_by("contest__problem__order")
                .values_list("problem__name", "ac_rate"),
            ),
            "problem_point": [
                get_histogram(problem_points[i]) for i in range(num_problems)
            ],
            "language_count": get_pie_chart(
                queryset.values("language__name")
                .annotate(count=Count("language__name"))
                .filter(count__gt=0)
                .order_by("-count")
                .values_list("language__name", "count"),
            ),
            "language_ac_rate": get_bar_chart(
                queryset.values("language__name")
                .annotate(ac_rate=ac_rate)
                .filter(ac_rate__gt=0)
                .values_list("language__name", "ac_rate"),
            ),
        }

        context["stats"] = mark_safe(json.dumps(stats))
        context["problems"] = labels
        return context


ContestRankingProfile = namedtuple(
    "ContestRankingProfile",
    "id user points cumtime tiebreaker participation "
    "participation_rating problem_cells result_cell",
)

BestSolutionData = namedtuple("BestSolutionData", "code points time state is_pretested")


def make_contest_ranking_profile(
    contest, participation, contest_problems, show_final=False
):
    if not show_final:
        points = participation.score
        cumtime = participation.cumtime
    else:
        points = participation.score_final
        cumtime = participation.cumtime_final

    user = participation.user
    return ContestRankingProfile(
        id=user.id,
        user=user,
        points=points,
        cumtime=cumtime,
        tiebreaker=participation.tiebreaker,
        participation_rating=participation.rating.rating
        if hasattr(participation, "rating")
        else None,
        problem_cells=[
            contest.format.display_user_problem(
                participation, contest_problem, show_final
            )
            for contest_problem in contest_problems
        ],
        result_cell=contest.format.display_participation_result(
            participation, show_final
        ),
        participation=participation,
    )


def base_contest_ranking_list(contest, problems, queryset, show_final=False):
    participation_fields = [
        field.name
        for field in ContestParticipation._meta.get_fields()
        if field.concrete and not field.many_to_many
    ]
    fields_to_fetch = participation_fields + [
        "user__id",
        "rating__rating",
    ]

    res = [
        make_contest_ranking_profile(contest, participation, problems, show_final)
        for participation in queryset.select_related("user", "rating").only(
            *fields_to_fetch
        )
    ]
    Profile.prefetch_profile_cache([p.id for p in res])
    return res


def contest_ranking_list(contest, problems, queryset=None, show_final=False):
    if queryset is None:
        queryset = contest.users.filter(virtual=0)

    if not show_final:
        return base_contest_ranking_list(
            contest,
            problems,
            queryset.extra(select={"round_score": "round(score, 6)"}).order_by(
                "is_disqualified", "-round_score", "cumtime", "tiebreaker"
            ),
            show_final,
        )
    else:
        return base_contest_ranking_list(
            contest,
            problems,
            queryset.extra(select={"round_score": "round(score_final, 6)"}).order_by(
                "is_disqualified", "-round_score", "cumtime_final", "tiebreaker"
            ),
            show_final,
        )


def get_contest_ranking_list(
    request,
    contest,
    participation=None,
    ranking_list=contest_ranking_list,
    show_current_virtual=False,
    ranker=ranker,
    show_final=False,
):
    problems = list(
        contest.contest_problems.select_related("problem")
        .defer("problem__description")
        .order_by("order")
    )

    users = ranker(
        ranking_list(contest, problems, show_final=show_final),
        key=attrgetter("points", "cumtime", "tiebreaker"),
    )

    if show_current_virtual:
        if participation is None and request.user.is_authenticated:
            participation = request.profile.current_contest
            if participation is None or participation.contest_id != contest.id:
                participation = None
        if participation is not None and participation.virtual:
            users = chain(
                [("-", make_contest_ranking_profile(contest, participation, problems))],
                users,
            )
    return users, problems


def contest_ranking_ajax(request, contest, participation=None):
    contest, exists = _find_contest(request, contest)
    show_final = bool(request.GET.get("final", False))
    if not exists:
        return HttpResponseBadRequest("Invalid contest", content_type="text/plain")

    if not contest.can_see_full_scoreboard(request.user):
        raise Http404()

    if show_final:
        if (
            not contest.is_editable_by(request.user)
            or not contest.format.has_hidden_subtasks
        ):
            raise Http404()

    queryset = contest.users.filter(virtual__gte=0)
    if request.GET.get("friend") == "true" and request.profile:
        friends = request.profile.get_friends()
        queryset = queryset.filter(user_id__in=friends)
    if request.GET.get("virtual") != "true":
        queryset = queryset.filter(virtual=0)

    users, problems = get_contest_ranking_list(
        request,
        contest,
        participation,
        ranking_list=partial(contest_ranking_list, queryset=queryset),
        show_final=show_final,
    )
    return render(
        request,
        "contest/ranking-table.html",
        {
            "users": users,
            "problems": problems,
            "contest": contest,
            "has_rating": contest.ratings.exists(),
            "can_edit": contest.is_editable_by(request.user),
        },
    )


class ContestRankingBase(ContestMixin, TitleMixin, DetailView):
    template_name = "contest/ranking.html"
    page_type = None

    def get_title(self):
        raise NotImplementedError()

    def get_content_title(self):
        return self.object.name

    def get_ranking_list(self):
        raise NotImplementedError()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not self.object.can_see_own_scoreboard(self.request.user):
            raise Http404()

        users, problems = self.get_ranking_list()
        context["users"] = users
        context["problems"] = problems
        context["page_type"] = self.page_type
        return context


class ContestRanking(ContestRankingBase):
    page_type = "ranking"

    def should_bypass_access_check(self, contest):
        return contest.public_scoreboard

    def get_title(self):
        return _("%s Rankings") % self.object.name

    def get_ranking_list(self):
        if not self.object.can_see_full_scoreboard(self.request.user):
            queryset = self.object.users.filter(
                user=self.request.profile, virtual=ContestParticipation.LIVE
            )
            return get_contest_ranking_list(
                self.request,
                self.object,
                ranking_list=partial(base_contest_ranking_list, queryset=queryset),
                ranker=lambda users, key: ((_("???"), user) for user in users),
            )

        return get_contest_ranking_list(self.request, self.object)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["has_rating"] = self.object.ratings.exists()
        return context


class ContestFinalRanking(LoginRequiredMixin, ContestRanking):
    page_type = "final_ranking"

    def get_ranking_list(self):
        if not self.object.is_editable_by(self.request.user):
            raise Http404()
        if not self.object.format.has_hidden_subtasks:
            raise Http404()
        return get_contest_ranking_list(self.request, self.object, show_final=True)


class ContestParticipationList(LoginRequiredMixin, ContestRankingBase):
    page_type = "participation"

    def get_title(self):
        if self.profile == self.request.profile:
            return _("Your participation in %s") % self.object.name
        return _("%s's participation in %s") % (self.profile.username, self.object.name)

    def get_ranking_list(self):
        if (
            not self.object.can_see_full_scoreboard(self.request.user)
            and self.profile != self.request.profile
        ):
            raise Http404()

        queryset = self.object.users.filter(user=self.profile, virtual__gte=0).order_by(
            "-virtual"
        )
        live_link = format_html(
            '<a href="{2}#!{1}">{0}</a>',
            _("Live"),
            self.profile.username,
            reverse("contest_ranking", args=[self.object.key]),
        )

        return get_contest_ranking_list(
            self.request,
            self.object,
            show_current_virtual=False,
            ranking_list=partial(base_contest_ranking_list, queryset=queryset),
            ranker=lambda users, key: (
                (user.participation.virtual or live_link, user) for user in users
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["has_rating"] = False
        context["now"] = timezone.now()
        context["rank_header"] = _("Participation")
        context["participation_tab"] = True
        return context

    def get(self, request, *args, **kwargs):
        if "user" in kwargs:
            self.profile = get_object_or_404(Profile, user__username=kwargs["user"])
        else:
            self.profile = self.request.profile
        return super().get(request, *args, **kwargs)


class ContestParticipationDisqualify(ContestMixin, SingleObjectMixin, View):
    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if not contest.is_editable_by(self.request.user):
            raise Http404()
        return contest

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        try:
            participation = self.object.users.get(pk=request.POST.get("participation"))
        except ObjectDoesNotExist:
            pass
        else:
            participation.set_disqualified(not participation.is_disqualified)
        return HttpResponseRedirect(reverse("contest_ranking", args=(self.object.key,)))


class ContestMossMixin(ContestMixin, PermissionRequiredMixin):
    permission_required = "judge.moss_contest"

    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if settings.MOSS_API_KEY is None or not contest.is_editable_by(
            self.request.user
        ):
            raise Http404()
        if not contest.is_editable_by(self.request.user):
            raise Http404()
        return contest


class ContestMossView(ContestMossMixin, TitleMixin, DetailView):
    template_name = "contest/moss.html"

    def get_title(self):
        return _("%s MOSS Results") % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        problems = list(
            map(
                attrgetter("problem"),
                self.object.contest_problems.order_by("order").select_related(
                    "problem"
                ),
            )
        )
        languages = list(map(itemgetter(0), ContestMoss.LANG_MAPPING))

        results = ContestMoss.objects.filter(contest=self.object)
        moss_results = defaultdict(list)
        for result in results:
            moss_results[result.problem].append(result)

        for result_list in moss_results.values():
            result_list.sort(key=lambda x: languages.index(x.language))

        context["languages"] = languages
        context["has_results"] = results.exists()
        context["moss_results"] = [
            (problem, moss_results[problem]) for problem in problems
        ]

        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        status = run_moss.delay(self.object.key)
        return redirect_to_task_status(
            status,
            message=_("Running MOSS for %s...") % (self.object.name,),
            redirect=reverse("contest_moss", args=(self.object.key,)),
        )


class ContestMossDelete(ContestMossMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        ContestMoss.objects.filter(contest=self.object).delete()
        return HttpResponseRedirect(reverse("contest_moss", args=(self.object.key,)))


class ContestTagDetailAjax(DetailView):
    model = ContestTag
    slug_field = slug_url_kwarg = "name"
    context_object_name = "tag"
    template_name = "contest/tag-ajax.html"


class ContestTagDetail(TitleMixin, ContestTagDetailAjax):
    template_name = "contest/tag.html"

    def get_title(self):
        return _("Contest tag: %s") % self.object.name


class ContestProblemClarificationForm(forms.Form):
    body = forms.CharField(
        widget=HeavyPreviewPageDownWidget(
            preview=reverse_lazy("comment_preview"),
            preview_timeout=1000,
            hide_preview_button=True,
        )
    )

    def __init__(self, request, *args, **kwargs):
        self.request = request
        super(ContestProblemClarificationForm, self).__init__(*args, **kwargs)
        self.fields["body"].widget.attrs.update({"placeholder": _("Issue description")})


class NewContestClarificationView(ContestMixin, TitleMixin, SingleObjectFormView):
    form_class = ContestProblemClarificationForm
    template_name = "contest/clarification.html"

    def get_form_kwargs(self):
        kwargs = super(NewContestClarificationView, self).get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def is_accessible(self):
        if not self.request.user.is_authenticated:
            return False
        if not self.request.in_contest:
            return False
        if not self.request.participation.contest == self.get_object():
            return False
        return self.get_object().is_editable_by(self.request.user)

    def get(self, request, *args, **kwargs):
        if not self.is_accessible():
            raise Http404()
        return super().get(self, request, *args, **kwargs)

    def form_valid(self, form):
        problem_code = self.request.POST["problem"]
        description = form.cleaned_data["body"]

        clarification = ContestProblemClarification(description=description)
        clarification.problem = get_object_or_404(
            ContestProblem, contest=self.get_object(), problem__code=problem_code
        )
        clarification.save()

        return HttpResponseRedirect(reverse("problem_list"))

    def get_title(self):
        return "New clarification for %s" % self.object.name

    def get_content_title(self):
        return mark_safe(
            escape(_("New clarification for %s"))
            % format_html(
                '<a href="{0}">{1}</a>',
                reverse("problem_detail", args=[self.object.key]),
                self.object.name,
            )
        )

    def get_context_data(self, **kwargs):
        context = super(NewContestClarificationView, self).get_context_data(**kwargs)
        context["problems"] = ContestProblem.objects.filter(
            contest=self.object
        ).order_by("order")
        return context


class ContestClarificationAjax(ContestMixin, DetailView):
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.is_accessible_by(request.user):
            raise Http404()

        polling_time = 1  # minute
        last_one_minute = timezone.now() - timezone.timedelta(minutes=polling_time)

        queryset = ContestProblemClarification.objects.filter(
            problem__in=self.object.contest_problems.all(), date__gte=last_one_minute
        )

        problems = list(
            ContestProblem.objects.filter(contest=self.object)
            .order_by("order")
            .values_list("problem__code", flat=True)
        )
        res = []
        for clarification in queryset:
            value = {
                "order": self.object.get_label_for_problem(
                    problems.index(clarification.problem.problem.code)
                ),
                "problem__name": clarification.problem.problem.name,
                "description": clarification.description,
            }
            res.append(value)

        return JsonResponse(res, safe=False, json_dumps_params={"ensure_ascii": False})


def update_contest_mode(request):
    if not request.is_ajax() or not request.method == "POST":
        return HttpResponseNotAllowed(["POST"])

    old_mode = request.session.get("contest_mode", True)
    request.session["contest_mode"] = not old_mode
    return HttpResponse()


ContestsSummaryData = namedtuple(
    "ContestsSummaryData",
    "username first_name last_name points point_contests css_class",
)


class ContestsSummaryView(DiggPaginatorMixin, ListView):
    paginate_by = 50
    template_name = "contest/contests_summary.html"

    def get(self, *args, **kwargs):
        try:
            self.contests_summary = ContestsSummary.objects.get(key=kwargs["key"])
        except:
            raise Http404()
        return super().get(*args, **kwargs)

    def get_queryset(self):
        total_rank = self.contests_summary.results
        return total_rank

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["contests"] = self.contests_summary.contests.all()
        context["title"] = _("Contests")
        context["first_page_href"] = "."
        return context


def recalculate_contest_summary_result(contest_summary):
    scores_system = contest_summary.scores
    contests = contest_summary.contests.all()
    total_points = defaultdict(int)
    result_per_contest = defaultdict(lambda: [(0, 0)] * len(contests))
    user_css_class = {}

    for i in range(len(contests)):
        contest = contests[i]
        users, problems = get_contest_ranking_list(None, contest)
        for rank, user in users:
            curr_score = 0
            if rank - 1 < len(scores_system):
                curr_score = scores_system[rank - 1]
            total_points[user.user] += curr_score
            result_per_contest[user.user][i] = (curr_score, rank)
            user_css_class[user.user] = user.css_class

    sorted_total_points = [
        ContestsSummaryData(
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            points=total_points[user],
            point_contests=result_per_contest[user],
            css_class=user_css_class[user],
        )
        for user in total_points
    ]

    sorted_total_points.sort(key=lambda x: x.points, reverse=True)
    total_rank = ranker(sorted_total_points)
    return [(rank, item._asdict()) for rank, item in total_rank]
