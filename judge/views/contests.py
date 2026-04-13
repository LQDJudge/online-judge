import csv
import io
from copy import deepcopy
import json
import math
from calendar import Calendar, SUNDAY
from collections import defaultdict, namedtuple
from datetime import date, datetime, time, timedelta
from functools import partial
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
    FloatField,
    IntegerField,
    Max,
    Min,
    Prefetch,
    Q,
    Value,
    When,
)
from django.db.models.expressions import CombinedExpression
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
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
from judge.views.comment import CommentableMixin
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
    OfficialContestCategory,
    OfficialContestLocation,
    Course,
    CourseContest,
)
from judge.models.course import EDITABLE_ROLES
from judge.models.contest import get_contest_problem_ids
from judge.tasks import run_moss
from judge.utils.celery import redirect_to_task_status
from judge.utils.opengraph import generate_opengraph
from judge.utils.problems import _get_result_data
from judge.views.problem import SolvedProblemMixin
from judge.utils.ranker import ranker
from judge.utils.stats import get_bar_chart, get_pie_chart, get_histogram
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.views import (
    DiggPaginatorMixin,
    QueryStringSortMixin,
    SingleObjectFormView,
    TitleMixin,
    generic_message,
    paginate_query_context,
)
from judge.widgets import HeavyPreviewPageDownWidget
from judge.views.pagevote import PageVoteDetailView
from judge.views.bookmark import BookMarkDetailView


__all__ = [
    "ContestList",
    "ContestDetail",
    "ContestProblems",
    "ContestRanking",
    "ContestJoin",
    "ContestLeave",
    "ContestCalendar",
    "ContestClone",
    "ContestStats",
    "ContestMossView",
    "ContestMossDelete",
    "ContestParticipationList",
    "ContestParticipationDisqualify",
    "get_ranking_queryset",
    "get_contest_problems",
    "build_ranking_profiles",
    "ContestClarificationView",
    "OfficialContestList",
    "RecommendedContestList",
    "ContestProblemset",
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
    official = False

    def get_queryset(self):
        q = Contest.get_visible_contests(self.request.user)
        if self.official:
            q = q.filter(official__isnull=False).select_related(
                "official", "official__category", "official__location"
            )
        else:
            q = q.filter(official__isnull=True)
        return q


class ContestList(
    QueryStringSortMixin, DiggPaginatorMixin, TitleMixin, ContestListMixin, ListView
):
    model = Contest
    paginate_by = 10
    template_name = "contest/list.html"
    title = gettext_lazy("Contests")
    all_sorts = frozenset(("name", "user_count", "start_time"))
    default_desc = frozenset(("name", "user_count"))
    context_object_name = "contests"

    def get_default_sort_order(self, request):
        if request.GET.get("contest") and settings.ENABLE_FTS:
            return "-relevance"
        if self.current_tab == "future":
            return "start_time"
        return "-start_time"

    @cached_property
    def _now(self):
        return timezone.now()

    def GET_with_session(self, request, key):
        if not request.GET.get(key):
            return request.session.get(key, False)
        return request.GET.get(key, None) == "1"

    def setup_contest_list(self, request):
        self.contest_query = request.GET.get("contest", "")

        self.hide_organization_contests = 0
        if self.GET_with_session(request, "hide_organization_contests"):
            self.hide_organization_contests = 1

        self.show_only_rated_contests = 0
        if self.GET_with_session(request, "show_only_rated_contests"):
            self.show_only_rated_contests = 1

        self.org_query = []
        if request.GET.get("orgs") and request.profile:
            try:
                self.org_query = list(map(int, request.GET.getlist("orgs")))
                if not request.user.is_superuser:
                    self.org_query = [
                        i
                        for i in self.org_query
                        if i
                        in set(
                            request.profile.organizations.values_list("id", flat=True)
                        )
                    ]
            except ValueError:
                pass

    def get(self, request, *args, **kwargs):
        default_tab = "active"
        if not self.request.user.is_authenticated:
            default_tab = "current"

        self.current_tab = self.request.GET.get("tab", default_tab)

        self.setup_contest_list(request)

        return super(ContestList, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        to_update = ("hide_organization_contests", "show_only_rated_contests")
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == "1"
                request.session[key] = val
            else:
                request.session[key] = False
        return HttpResponseRedirect(request.get_full_path())

    def extra_queryset_filters(self, queryset):
        return queryset

    def _get_queryset(self):
        queryset = (
            super(ContestList, self)
            .get_queryset()
            .prefetch_related(
                "tags",
                Prefetch(
                    "course",
                    queryset=CourseContest.objects.select_related("course"),
                ),
            )
        )

        if self.contest_query:
            substr_queryset = queryset.filter(
                Q(key__icontains=self.contest_query)
                | Q(name__icontains=self.contest_query)
            )
            if settings.ENABLE_FTS:
                queryset = (
                    queryset.search(self.contest_query).extra(order_by=["-relevance"])
                    | substr_queryset
                )
            else:
                queryset = substr_queryset
        if not self.org_query and self.request.organization:
            self.org_query = [self.request.organization.id]
        if self.hide_organization_contests:
            queryset = queryset.filter(organizations=None, is_in_course=False)
        if self.show_only_rated_contests:
            queryset = queryset.filter(is_rated=True)
        if self.org_query:
            queryset = queryset.filter(organizations__in=self.org_query)
        queryset = self.extra_queryset_filters(queryset)
        return queryset

    def _get_past_contests_queryset(self):
        return (
            self._get_queryset()
            .filter(end_time__lt=self._now)
            .order_by(self.order, "key")
        )

    @cached_property
    def _recommended_contests_queryset(self):
        """Get recommended contests for the current user. Computed once per request."""
        from judge.utils.contest_recommendation import (
            get_recommended_contests,
            get_recommended_contests_for_anonymous,
        )

        if self.request.user.is_authenticated and self.request.profile:
            scored = get_recommended_contests(self.request.profile, limit=100)
            if scored:
                contest_ids = [cid for cid, _ in scored]
                preserved = Case(
                    *[When(pk=pk, then=pos) for pos, pk in enumerate(contest_ids)]
                )
                return Contest.objects.filter(id__in=contest_ids).order_by(preserved)
        contest_ids = get_recommended_contests_for_anonymous(limit=100)
        return Contest.objects.filter(id__in=contest_ids).order_by("-user_count")

    def _active_participations(self):
        return ContestParticipation.objects.filter(
            virtual=0,
            user=self.request.profile,
            contest__start_time__lte=self._now,
            contest__end_time__gte=self._now,
        )

    @cached_property
    def _active_contests_ids(self):
        return [
            participation.contest_id
            for participation in self._active_participations().select_related("contest")
            if not participation.ended
        ]

    def _get_current_contests_queryset(self):
        return (
            self._get_queryset()
            .exclude(id__in=self._active_contests_ids)
            .filter(start_time__lte=self._now, end_time__gte=self._now)
            .order_by(self.order, "key")
        )

    def _get_future_contests_queryset(self):
        return (
            self._get_queryset()
            .filter(start_time__gt=self._now)
            .order_by(self.order, "key")
        )

    def _get_active_participations_queryset(self):
        active_contests = (
            self._get_queryset()
            .filter(id__in=self._active_contests_ids)
            .order_by(self.order, "key")
        )
        ordered_ids = list(active_contests.values_list("id", flat=True))

        participations = self._active_participations().filter(
            contest_id__in=ordered_ids
        )
        participations = sorted(
            participations, key=lambda p: ordered_ids.index(p.contest_id)
        )
        return participations

    def get_queryset(self):
        # If no specific tab is requested and user is authenticated, check if we should default to current instead of active
        if (
            self.current_tab == "active"
            and not self.request.GET.get("tab")
            and self.request.user.is_authenticated
        ):
            active_participations = self._get_active_participations_queryset()
            if len(active_participations) == 0:
                # Switch to current tab since there are no active contests
                self.current_tab = "current"
                return self._get_current_contests_queryset()
            else:
                return active_participations

        if self.current_tab == "past":
            return self._get_past_contests_queryset()
        elif self.current_tab == "current":
            return self._get_current_contests_queryset()
        elif self.current_tab == "future":
            return self._get_future_contests_queryset()
        else:  # Default to active
            return self._get_active_participations_queryset()

    def get_context_data(self, **kwargs):
        context = super(ContestList, self).get_context_data(**kwargs)

        context["current_tab"] = self.current_tab

        context["current_count"] = self._get_current_contests_queryset().count()
        context["future_count"] = self._get_future_contests_queryset().count()
        context["active_count"] = len(self._get_active_participations_queryset())
        context["now"] = self._now
        context["first_page_href"] = "."
        context["contest_query"] = self.contest_query
        context["org_query"] = self.org_query
        context["hide_organization_contests"] = int(self.hide_organization_contests)
        context["show_only_rated_contests"] = int(self.show_only_rated_contests)
        if self.request.profile:
            context["organizations"] = self.request.profile.get_organizations()
        context["page_type"] = "list"
        context["selected_order"] = self.request.GET.get("order")
        context["all_sort_options"] = [
            ("start_time", _("Start time (asc.)")),
            ("-start_time", _("Start time (desc.)")),
            ("name", _("Name (asc.)")),
            ("-name", _("Name (desc.)")),
            ("user_count", _("User count (asc.)")),
            ("-user_count", _("User count (desc.)")),
        ]
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        Contest.prefetch_organization_ids(
            *[contest.id for contest in context["contests"]]
        )
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
                context["live_participation"] = (
                    self.request.profile.contest_history.get(
                        contest=self.object,
                        virtual=ContestParticipation.LIVE,
                    )
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
        has_hidden_results = self.object.contest_problems.filter(
            is_result_hidden=True
        ).exists()
        context["show_final_ranking"] = (
            self.object.format.has_hidden_subtasks or has_hidden_results
        ) and self.object.is_editable_by(self.request.user)
        context["logo_override_image"] = self.object.logo_override_image
        context["organizations"] = self.object.get_organizations()
        context["is_clonable"] = is_contest_clonable(self.request, self.object)

        if not context["logo_override_image"] and len(context["organizations"]) > 0:
            org_image = context["organizations"][0].get_organization_image_url()
            if org_image:
                context["logo_override_image"] = org_image

        return context

    def contest_access_check(self, contest):
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

        self.contest_access_check(contest)

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
    SolvedProblemMixin,
    CommentableMixin,
    DetailView,
    PageVoteDetailView,
    BookMarkDetailView,
):
    template_name = "contest/contest.html"

    def get_title(self):
        return self.object.name

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile

    @cached_property
    def in_contest(self):
        return self.request.in_contest

    def _is_editable_organization(self, organization):
        if self.request.profile.can_edit_organization(organization):
            return True
        if self.request.profile in organization and self.object.is_editable_by(
            self.request.user
        ):
            return True
        return False

    def get_editable_organizations(self):
        if not self.request.profile:
            return []
        res = []
        for organization in self.object.get_organizations():
            if self._is_editable_organization(organization):
                res.append(organization)
        return res

    def get_context_data(self, **kwargs):
        context = super(ContestDetail, self).get_context_data(**kwargs)
        contest_problem_ids = get_contest_problem_ids(self.object.id)
        Problem.prefetch_cache_i18n_name(
            self.request.LANGUAGE_CODE, *contest_problem_ids
        )
        context["contest_problems"] = Problem.get_cached_instances(*contest_problem_ids)
        context["problems"] = context["contest_problems"]
        context["editable_organizations"] = self.get_editable_organizations()

        # Get quizzes in this contest
        contest_quizzes = (
            ContestProblem.objects.filter(contest=self.object, quiz__isnull=False)
            .select_related("quiz")
            .order_by("order")
        )
        context["contest_quizzes"] = contest_quizzes
        context["result_hidden_contest_quiz_ids"] = set(
            cq.id for cq in contest_quizzes if cq.is_result_hidden
        )

        if self.object.is_in_course:
            course = CourseContest.get_course_of_contest(self.object)
            context["course"] = course
            context["is_editable_course"] = Course.is_editable_by(
                course, self.request.profile
            )

        is_in_viewed_contest = (
            self.request.in_contest
            and self.request.participation.contest_id == self.object.id
        )
        context["current_contest"] = (
            self.request.participation.contest if is_in_viewed_contest else None
        )

        # User's quiz attempt data for display
        if self.profile and is_in_viewed_contest:
            from judge.models.quiz import QuizAttempt

            quiz_user_data = {}
            for cq in context["contest_quizzes"]:
                attempts = QuizAttempt.objects.filter(
                    quiz=cq.quiz,
                    user=self.profile,
                    contest_participation=self.request.participation,
                    is_submitted=True,
                )
                best = attempts.order_by("-score").first()
                contest_score = None
                if best and best.score is not None and best.max_score:
                    contest_score = (
                        float(best.score) / float(best.max_score) * cq.points
                    )
                quiz_user_data[cq.quiz.id] = {
                    "best_score": contest_score,
                    "best_attempt_id": best.id if best else None,
                    "attempt_count": attempts.count(),
                }
            context["quiz_user_data"] = quiz_user_data

        context["has_hidden_subtasks"] = self.object.format.has_hidden_subtasks
        context["hide_contest_scoreboard"] = self.object.scoreboard_visibility in (
            self.object.SCOREBOARD_AFTER_CONTEST,
            self.object.SCOREBOARD_AFTER_PARTICIPATION,
        )

        # Per-problem result hiding
        if not self.object.is_editable_by(self.request.user):
            context["result_hidden_problem_ids"] = set(
                self.object.contest_problems.filter(
                    is_result_hidden=True, problem__isnull=False
                ).values_list("problem_id", flat=True)
            )
        else:
            context["result_hidden_problem_ids"] = set()

        if self.profile:
            if is_in_viewed_contest:
                context["completed_problem_ids"] = self.get_completed_problems()
                context["attempted_problems"] = self.get_attempted_problems()
            else:
                from judge.utils.problems import user_attempted_ids, user_completed_ids

                context["completed_problem_ids"] = user_completed_ids(self.profile)
                context["attempted_problems"] = user_attempted_ids(self.profile)

        # Clarifications
        if self.object.use_clarifications:
            context["clarifications"] = (
                ContestProblemClarification.objects.filter(problem__contest=self.object)
                .select_related("problem__problem")
                .order_by("-date")
            )

        context = self.get_comment_context(context)

        return context


class ContestProblems(ContestMixin, SolvedProblemMixin, TitleMixin, DetailView):
    template_name = "contest/problems.html"

    def get_title(self):
        return _("Problems in %s") % self.object.name

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        contest = self.object
        contest_problem_ids = get_contest_problem_ids(contest.id)
        Problem.prefetch_cache_i18n_name(
            self.request.LANGUAGE_CODE, *contest_problem_ids
        )
        context["contest_problems"] = Problem.get_cached_instances(*contest_problem_ids)
        context["problems"] = context["contest_problems"]

        # Quizzes
        contest_quizzes = (
            ContestProblem.objects.filter(contest=contest, quiz__isnull=False)
            .select_related("quiz")
            .order_by("order")
        )
        context["contest_quizzes"] = contest_quizzes
        context["result_hidden_contest_quiz_ids"] = set(
            cq.id for cq in contest_quizzes if cq.is_result_hidden
        )

        # User's quiz attempt data for display
        is_in_contest = contest.is_in_contest(self.request.user)
        if self.profile and is_in_contest and self.request.in_contest:
            from judge.models.quiz import QuizAttempt

            quiz_user_data = {}
            for cq in contest_quizzes:
                attempts = QuizAttempt.objects.filter(
                    quiz=cq.quiz,
                    user=self.profile,
                    contest_participation=self.request.participation,
                    is_submitted=True,
                )
                best = attempts.order_by("-score").first()
                contest_score = None
                if best and best.score is not None and best.max_score:
                    contest_score = (
                        float(best.score) / float(best.max_score) * cq.points
                    )
                quiz_user_data[cq.quiz.id] = {
                    "best_score": contest_score,
                    "best_attempt_id": best.id if best else None,
                    "attempt_count": attempts.count(),
                }
            context["quiz_user_data"] = quiz_user_data

        # Determine if user is actively in this contest (live or virtual)
        context["is_in_contest"] = is_in_contest
        context["current_contest"] = (
            self.request.participation.contest
            if (
                self.request.in_contest
                and self.request.participation.contest_id == contest.id
            )
            else None
        )

        context["has_hidden_subtasks"] = contest.format.has_hidden_subtasks
        context["hide_contest_scoreboard"] = contest.scoreboard_visibility in (
            contest.SCOREBOARD_AFTER_CONTEST,
            contest.SCOREBOARD_AFTER_PARTICIPATION,
        )

        # Per-problem result hiding
        if not contest.is_editable_by(self.request.user):
            context["result_hidden_problem_ids"] = set(
                contest.contest_problems.filter(
                    is_result_hidden=True, problem__isnull=False
                ).values_list("problem_id", flat=True)
            )
        else:
            context["result_hidden_problem_ids"] = set()

        if self.profile:
            if is_in_contest:
                context["completed_problem_ids"] = self.get_completed_problems()
                context["attempted_problems"] = self.get_attempted_problems()
            else:
                from judge.utils.problems import user_attempted_ids, user_completed_ids

                context["completed_problem_ids"] = user_completed_ids(self.profile)
                context["attempted_problems"] = user_attempted_ids(self.profile)

        # Clarifications
        if contest.use_clarifications:
            context["clarifications"] = (
                ContestProblemClarification.objects.filter(problem__contest=contest)
                .select_related("problem__problem")
                .order_by("-date")
            )

        return context


def is_contest_clonable(request, contest):
    if not request.profile:
        return False

    if (
        not request.profile.get_admin_organization_ids()
        and not Course.objects.filter(
            courserole__user=request.profile,
            courserole__role__in=EDITABLE_ROLES,
        ).exists()
    ):
        return False

    if request.user.has_perm("judge.clone_contest"):
        return True
    if contest.access_code and not contest.is_editable_by(request.user):
        return False
    if (
        contest.end_time is not None
        and contest.end_time + timedelta(days=1) < contest._now
    ):
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
        kwargs["course_choices"] = tuple(
            Course.objects.filter(
                courserole__user=self.request.profile,
                courserole__role__in=EDITABLE_ROLES,
            ).values_list("id", "name")
        )
        kwargs["profile"] = self.request.profile
        return kwargs

    def form_valid(self, form):
        tags = self.object.tags.all()
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
        contest.private_contestants.set(private_contestants)
        contest.view_contest_scoreboard.set(view_contest_scoreboard)
        contest.authors.add(self.request.profile)

        target_type = form.cleaned_data["target_type"]
        if target_type == "organization":
            contest.is_in_course = False
            organization = form.cleaned_data["organization"]
            contest.organizations.set([organization])
            redirect_url = reverse(
                "organization_contest_edit",
                args=(organization.id, organization.slug, contest.key),
            )
        elif target_type == "course":
            course = form.cleaned_data["course"]
            contest.is_in_course = True
            contest.organizations.clear()

            # Create a CourseContest entry that links the cloned contest to the course
            CourseContest.objects.create(
                course=course,
                contest=contest,
                order=CourseContest.objects.filter(course=course).count() + 1,
                points=0,  # Default points, can be adjusted as needed
            )

            redirect_url = reverse(
                "edit_course_contest",
                args=(course.slug, contest.key),
            )
        else:
            raise Http404("Invalid target type selected.")

        for problem in contest_problems:
            problem.contest = contest
            problem.pk = None
        ContestProblem.objects.bulk_create(contest_problems)

        contest.save()

        return HttpResponseRedirect(redirect_url)


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
            profile.remove_contest()

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
        return HttpResponseRedirect(reverse("contest_problems", args=(contest.key,)))

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
            if max_point > 0:
                bin_idx = math.floor(point * self.POINT_BIN / max_point)
            else:
                bin_idx = 0
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


def get_ranking_queryset(contest, queryset=None, show_final=False):
    """Return an ordered ContestParticipation queryset."""
    if queryset is None:
        queryset = contest.users.filter(virtual=0)
    if show_final:
        return queryset.order_by(
            "is_disqualified", "-score_final", "cumtime_final", "tiebreaker", "id"
        )
    return queryset.order_by("is_disqualified", "-score", "cumtime", "tiebreaker", "id")


def get_contest_problems(contest):
    """Fetch contest problems. Problem data accessed via cache (no JOIN)."""
    problems = list(contest.contest_problems.select_related("quiz").order_by("order"))
    # Pre-populate Django's FK cache from CacheableModel cache,
    # so contest_problem.problem.code hits cache instead of DB
    problem_ids = [cp.problem_id for cp in problems if cp.problem_id]
    if problem_ids:
        cached = {p.id: p for p in Problem.get_cached_instances(*problem_ids)}
        for cp in problems:
            if cp.problem_id in cached:
                cp.problem = cached[cp.problem_id]
    return problems


def build_ranking_profiles(contest, problems, participations, show_final=False):
    """Convert participations into ContestRankingProfile list with rendered cells."""
    if not hasattr(contest, "_result_hidden_ids"):
        contest._result_hidden_ids = set(
            contest.contest_problems.filter(is_result_hidden=True).values_list(
                "id", flat=True
            )
        )
    result_hidden_ids = contest._result_hidden_ids if not show_final else set()

    # Ensure full objects are loaded with relations
    if hasattr(participations, "select_related"):
        participations = participations.select_related("user", "rating")

    res = []
    for participation in participations:
        points = participation.score_final if show_final else participation.score
        cumtime = participation.cumtime_final if show_final else participation.cumtime

        format_data = participation.format_data or {}
        problem_cells = []
        for cp in problems:
            if result_hidden_ids and cp.id in result_hidden_ids:
                key = f"quiz_{cp.id}" if cp.quiz_id else str(cp.id)
                if key in format_data:
                    cell = format_html(
                        '<td class="problem-score-col"><span>?</span></td>'
                    )
                else:
                    cell = contest.format.display_empty_cell(cp)
            else:
                cell = contest.format.display_user_problem(
                    participation, cp, show_final
                )
            problem_cells.append(cell)

        res.append(
            ContestRankingProfile(
                id=participation.user_id,
                user=participation.user,
                points=points,
                cumtime=cumtime,
                tiebreaker=participation.tiebreaker,
                participation_rating=(
                    participation.rating.rating
                    if hasattr(participation, "rating")
                    else None
                ),
                problem_cells=problem_cells,
                result_cell=contest.format.display_participation_result(
                    participation, show_final
                ),
                participation=participation,
            )
        )

    Profile.get_cached_instances(*[p.id for p in res])
    return res


def compute_ranks(rows, target_ids=None, include_position=False):
    """Compute ranks from ordered rows using ranker() tie logic.

    Args:
        rows: iterable of (id, score, cumtime, tiebreaker) tuples (already ordered)
        target_ids: if set, only return results for these IDs (and stop early if all found)
        include_position: if True, return {id: (rank, position)} instead of {id: rank}

    Returns:
        dict {id: rank} or {id: (rank, position)}
    """
    result = {}
    rank = 0
    delta = 1
    last_key = None
    remaining = set(target_ids) if target_ids else None

    for i, (pid, score, cumtime, tb) in enumerate(rows):
        key = (score, cumtime, tb)
        if key != last_key:
            rank += delta
            delta = 0
        delta += 1
        last_key = key

        if remaining is None or pid in remaining:
            result[pid] = (rank, i) if include_position else rank
            if remaining:
                remaining.discard(pid)
                if not remaining:
                    break

    return result


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
        has_hidden_results = self.object.contest_problems.filter(
            is_result_hidden=True
        ).exists()
        context["show_final_ranking"] = (
            self.object.format.has_hidden_subtasks or has_hidden_results
        ) and self.object.is_editable_by(self.request.user)
        return context


RANKING_PAGE_SIZE = 100


class ContestRanking(ContestRankingBase):
    page_type = "ranking"
    show_final = False

    def should_bypass_access_check(self, contest):
        return contest.public_scoreboard

    def get_title(self):
        return _("%s Rankings") % self.object.name

    def get(self, request, *args, **kwargs):
        if request.GET.get("format") == "csv":
            self.object = self.get_object()
            self.setup_filters()
            return self._render_csv()
        return super().get(request, *args, **kwargs)

    def _render_csv(self):
        contest = self.object
        if not contest.can_see_full_scoreboard(self.request.user):
            raise Http404()

        problems = get_contest_problems(contest)
        self.all_rows = self._get_lightweight_rows(self._get_base_queryset())
        filtered_rows = self._filter_rows(self.all_rows)
        filtered_ids = [r[0] for r in filtered_rows]
        qs = get_ranking_queryset(
            contest, contest.users.filter(id__in=filtered_ids), self.show_final
        )
        s = "score_final" if self.show_final else "score"

        # Fetch only what CSV needs: username, names, score, format_data
        participations = qs.select_related("user__user").only(
            "id",
            "user__user__username",
            "user__user__first_name",
            "user__user__last_name",
            s,
            "cumtime",
            "tiebreaker",
            "format_data",
        )

        # Compute ranks from lightweight rows
        rows = ((pid, s_, c_, tb) for pid, _uid, s_, c_, tb in filtered_rows)
        rank_map = compute_ranks(rows)

        output = io.StringIO()
        writer = csv.writer(output)

        header = [_("Rank"), _("Username"), _("Full Name"), _("School"), _("Score")]
        for cp in problems:
            header.append(contest.get_label_for_problem(cp.order))
        writer.writerow(header)

        for p in participations:
            fd = p.format_data or {}
            row = [
                rank_map.get(p.id, ""),
                p.user.user.username,
                p.user.user.first_name or "",
                p.user.user.last_name or "",
                getattr(p, s),
            ]
            for cp in problems:
                k = f"quiz_{cp.id}" if cp.quiz_id else str(cp.id)
                pdata = fd.get(k)
                row.append(pdata["points"] if pdata else "")
            writer.writerow(row)

        response = HttpResponse(output.getvalue(), content_type="text/csv")
        safe_key = contest.key.replace('"', "").replace(";", "")
        response["Content-Disposition"] = (
            f'attachment; filename="{safe_key}_ranking.csv"'
        )
        return response

    def _get_base_queryset(self):
        """Base queryset with virtual filter + search (DB-level)."""
        queryset = self.object.users
        if not self.include_virtual:
            queryset = queryset.filter(virtual=0)
        else:
            queryset = queryset.filter(virtual__gte=0)
        if self.search_query:
            queryset = queryset.filter(
                Q(user__user__username__icontains=self.search_query)
                | Q(user__user__first_name__icontains=self.search_query)
            )
        return queryset

    def _get_lightweight_rows(self, queryset):
        """Fetch ordered (id, user_id, score, cumtime, tiebreaker) tuples."""
        s = "score_final" if self.show_final else "score"
        c = "cumtime_final" if self.show_final else "cumtime"
        qs = get_ranking_queryset(self.object, queryset, self.show_final)
        return list(qs.values_list("id", "user_id", s, c, "tiebreaker"))

    def _filter_rows(self, rows):
        """Apply friend/favorites filters in Python."""
        if self.friend_only:
            followings = set(self.request.profile.get_following_ids(True))
            rows = [r for r in rows if r[1] in followings]
        if self.favorite_ids:
            fav_set = set(self.favorite_ids)
            rows = [r for r in rows if r[1] in fav_set]
        return rows

    def get_ranking_list(self):
        contest = self.object

        if not contest.can_see_full_scoreboard(self.request.user):
            qs = get_ranking_queryset(
                contest,
                contest.users.filter(
                    user=self.request.profile, virtual=ContestParticipation.LIVE
                ),
                self.show_final,
            )
            problems = get_contest_problems(contest)
            profiles = build_ranking_profiles(contest, problems, qs, self.show_final)
            users = ((_("???"), user) for user in profiles)
            return users, problems

        problems = get_contest_problems(contest)

        # One lightweight query for all participants (with search at DB level)
        self.all_rows = self._get_lightweight_rows(self._get_base_queryset())
        filtered_rows = self._filter_rows(self.all_rows)

        # Paginate in Python
        total = len(filtered_rows)
        page_number = 1

        # ?user=username → find their page
        highlight_user = self.request.GET.get("user", "").strip()
        if highlight_user:
            target_uid = (
                Profile.objects.filter(user__username=highlight_user)
                .values_list("id", flat=True)
                .first()
            )
            if target_uid:
                for i, (pid, uid, *_rest) in enumerate(filtered_rows):
                    if uid == target_uid:
                        page_number = (i // RANKING_PAGE_SIZE) + 1
                        self.highlight_username = highlight_user
                        break

        if not highlight_user:
            try:
                page_number = int(self.request.GET.get("page", 1))
            except ValueError:
                page_number = 1
        num_pages = max(1, (total + RANKING_PAGE_SIZE - 1) // RANKING_PAGE_SIZE)
        page_number = max(1, min(page_number, num_pages))

        start = (page_number - 1) * RANKING_PAGE_SIZE
        page_rows = filtered_rows[start : start + RANKING_PAGE_SIZE]

        # Fetch full objects for this page only
        page_ids = [row[0] for row in page_rows]
        qs = get_ranking_queryset(
            contest, contest.users.filter(id__in=page_ids), self.show_final
        )
        profiles = build_ranking_profiles(contest, problems, qs, self.show_final)

        users = ranker(
            profiles,
            key=attrgetter("points", "cumtime", "tiebreaker"),
            rank=start,
        )

        # Build page_obj for template pagination
        self.page_obj = DiggPaginator(
            filtered_rows, RANKING_PAGE_SIZE, body=3, tail=1, padding=1
        ).get_page(page_number)
        self.filtered_rows = filtered_rows

        return users, problems

    def _get_default_include_virtual(self):
        if hasattr(self.object, "official"):
            return "1"
        return "0"

    def setup_filters(self):
        if self.request.profile:
            self.friend_only = bool(self.request.GET.get("friend") == "1")
        else:
            self.friend_only = False
        self.include_virtual = bool(
            self.request.GET.get("virtual", self._get_default_include_virtual()) == "1"
        )
        self.search_query = self.request.GET.get("search", "").strip()
        self.ajax_only = bool(self.request.GET.get("ajax") == "1")
        self.page_obj = None
        self.all_rows = None
        self.filtered_rows = None
        self.highlight_username = None

        # Parse favorite user IDs (max 50, from localStorage via JS)
        self.favorite_ids = []
        fav_param = self.request.GET.get("favorites", "")
        if fav_param:
            for x in fav_param.split(",")[:50]:
                try:
                    self.favorite_ids.append(int(x))
                except ValueError:
                    pass

        if self.ajax_only:
            self.template_name = "contest/ranking-ajax.html"

    def _find_my_position(self):
        """Find current user's position and rank in filtered_rows (no extra queries)."""
        if not self.request.user.is_authenticated or not self.filtered_rows:
            return None
        my_pid = (
            self.object.users.filter(
                user=self.request.profile, virtual=ContestParticipation.LIVE
            )
            .values_list("id", flat=True)
            .first()
        )
        if not my_pid:
            return None

        rows = ((pid, s, c, tb) for pid, uid, s, c, tb in self.filtered_rows)
        ranks = compute_ranks(rows, target_ids={my_pid}, include_position=True)
        if my_pid not in ranks:
            return None
        rank, position = ranks[my_pid]
        return {
            "rank": rank,
            "page": (position // RANKING_PAGE_SIZE) + 1,
            "participation_id": my_pid,
        }

    def _compute_global_ranks(self, page_user_ids):
        """Compute overall ranks from all_rows (no extra queries)."""
        rows = ((uid, s, c, tb) for _pid, uid, s, c, tb in self.all_rows)
        return compute_ranks(rows, target_ids=page_user_ids)

    def get_context_data(self, **kwargs):
        self.setup_filters()
        context = super().get_context_data(**kwargs)
        context["has_rating"] = self.object.ratings.exists()
        context["search_query"] = self.search_query
        context["page_obj"] = self.page_obj
        if self.page_obj is not None:
            context.update(paginate_query_context(self.request))
            my_info = self._find_my_position()
            if my_info:
                context["my_page"] = my_info["page"]
                context["my_rank"] = my_info["rank"]
                if my_info["page"] != self.page_obj.number:
                    participation = (
                        self.object.users.filter(id=my_info["participation_id"])
                        .select_related("user", "rating")
                        .first()
                    )
                    if participation:
                        context["my_profile"] = build_ranking_profiles(
                            self.object,
                            context["problems"],
                            [participation],
                            self.show_final,
                        )[0]
            if self.friend_only or self.favorite_ids:
                start = (self.page_obj.number - 1) * RANKING_PAGE_SIZE
                end = self.page_obj.number * RANKING_PAGE_SIZE
                page_user_ids = set(row[1] for row in self.filtered_rows[start:end])
                context["global_ranks"] = self._compute_global_ranks(page_user_ids)
        context["highlight_username"] = self.highlight_username
        if not self.ajax_only:
            context["include_virtual"] = self.include_virtual
            context["friend_only"] = self.friend_only
            context["last_msg"] = event.last()
        return context


class ContestFinalRanking(LoginRequiredMixin, ContestRanking):
    page_type = "final_ranking"
    show_final = True

    def get_ranking_list(self):
        if not self.object.is_editable_by(self.request.user):
            raise Http404()
        has_hidden = (
            self.object.format.has_hidden_subtasks
            or self.object.contest_problems.filter(is_result_hidden=True).exists()
        )
        if not has_hidden:
            raise Http404()
        return super().get_ranking_list()


class ContestParticipationList(LoginRequiredMixin, ContestRankingBase):
    page_type = "participation"

    def get_title(self):
        if self.profile == self.request.profile:
            return _("Your participation in %s") % self.object.name
        return _("%(username)s's participation in %(contest)s") % {
            "username": self.profile.username,
            "contest": self.object.name,
        }

    def get_ranking_list(self):
        if (
            not self.object.can_see_full_scoreboard(self.request.user)
            and self.profile != self.request.profile
        ):
            raise Http404()

        contest = self.object
        qs = contest.users.filter(user=self.profile, virtual__gte=0).order_by(
            "-virtual"
        )

        problems = get_contest_problems(contest)
        profiles = build_ranking_profiles(contest, problems, qs)

        live_link = format_html(
            '<a href="{2}#!{1}">{0}</a>',
            _("Live"),
            self.profile.username,
            reverse("contest_ranking", args=[contest.key]),
        )
        users = ((user.participation.virtual or live_link, user) for user in profiles)
        return users, problems

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


class ContestBulkDisqualify(ContestMixin, SingleObjectMixin, View):
    def get_object(self, queryset=None):
        contest = super().get_object(queryset)
        if not contest.is_editable_by(self.request.user):
            raise Http404()
        return contest

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        usernames_raw = request.POST.get("usernames", "")
        # Split by comma, space, or newline and remove empty strings
        usernames = set()
        for part in usernames_raw.replace(",", " ").replace("\n", " ").split():
            username = part.strip()
            if username:
                usernames.add(username)

        disqualified_count = 0
        for username in usernames:
            # Use filter() instead of get() because a user can have multiple participations
            participations = self.object.users.filter(user__user__username=username)
            for participation in participations:
                if not participation.is_disqualified:
                    participation.set_disqualified(True)
                    disqualified_count += 1

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

        return HttpResponseRedirect(
            reverse("contest_view", args=(self.get_object().key,))
        )

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


ContestsSummaryData = namedtuple(
    "ContestsSummaryData",
    "user_id points point_contests",
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

        # Prefetch all user profiles using cached instances
        user_ids = [item[1]["user_id"] for item in context["object_list"]]
        profiles = {}
        if user_ids:
            # Get cached profile instances and create a lookup dictionary
            profiles = {p.id: p for p in Profile.get_cached_instances(*user_ids)}

        # Add profile lookup to context
        context["profiles"] = profiles

        return context


def recalculate_contest_summary_result(request, contest_summary):
    scores_system = contest_summary.scores
    contests = contest_summary.contests.all()
    total_points = defaultdict(int)
    result_per_contest = defaultdict(lambda: [(0, 0)] * len(contests))

    for i in range(len(contests)):
        contest = contests[i]
        problems = get_contest_problems(contest)
        qs = get_ranking_queryset(contest)
        profiles = build_ranking_profiles(contest, problems, qs)
        users = list(
            ranker(profiles, key=attrgetter("points", "cumtime", "tiebreaker"))
        )

        # Group users by rank and calculate sum of points for tied positions
        rank_groups = defaultdict(list)
        for rank, user in users:
            rank_groups[rank].append(user)

        # Calculate points for each rank group
        rank_points = {}
        for rank, group_users in rank_groups.items():
            num_users = len(group_users)
            # Sum the points for all positions occupied by tied users
            total_rank_points = 0
            for j in range(num_users):
                position_index = rank - 1 + j
                if position_index < len(scores_system):
                    total_rank_points += scores_system[position_index]
            # Divide the sum equally among all tied users
            rank_points[rank] = total_rank_points / num_users if num_users > 0 else 0

        # Assign calculated points to each user
        for rank, user in users:
            curr_score = rank_points[rank]
            total_points[user.user] += curr_score
            result_per_contest[user.user][i] = (curr_score, rank)

    sorted_total_points = [
        ContestsSummaryData(
            user_id=user.id,
            points=total_points[user],
            point_contests=result_per_contest[user],
        )
        for user in total_points
    ]

    sorted_total_points.sort(key=lambda x: x.points, reverse=True)
    total_rank = ranker(sorted_total_points)
    return [(rank, item._asdict()) for rank, item in total_rank]


class OfficialContestList(ContestList):
    official = True
    template_name = "contest/official_list.html"

    def setup_contest_list(self, request):
        self.contest_query = request.GET.get("contest", "")
        self.org_query = []
        self.hide_organization_contests = False
        self.show_only_rated_contests = False

        self.selected_categories = []
        self.selected_locations = []
        self.year_from = None
        self.year_to = None

        if "category" in request.GET:
            try:
                self.selected_categories = list(
                    map(int, request.GET.getlist("category"))
                )
            except ValueError:
                pass
        if "location" in request.GET:
            try:
                self.selected_locations = list(
                    map(int, request.GET.getlist("location"))
                )
            except ValueError:
                pass
        if "year_from" in request.GET:
            try:
                self.year_from = int(request.GET.get("year_from"))
            except ValueError:
                pass
        if "year_to" in request.GET:
            try:
                self.year_to = int(request.GET.get("year_to"))
            except ValueError:
                pass

    def extra_queryset_filters(self, queryset):
        if self.selected_categories:
            queryset = queryset.filter(official__category__in=self.selected_categories)
        if self.selected_locations:
            queryset = queryset.filter(official__location__in=self.selected_locations)
        if self.year_from:
            queryset = queryset.filter(official__year__gte=self.year_from)
        if self.year_to:
            queryset = queryset.filter(official__year__lte=self.year_to)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "official"
        context["is_official"] = True
        context["categories"] = OfficialContestCategory.objects.all()
        context["locations"] = OfficialContestLocation.objects.all()
        context["selected_categories"] = self.selected_categories
        context["selected_locations"] = self.selected_locations
        context["year_from"] = self.year_from
        context["year_to"] = self.year_to

        return context


class RecommendedContestList(ContestList):
    title = gettext_lazy("For you")
    template_name = "contest/recommended_list.html"

    def setup_contest_list(self, request):
        self.contest_query = request.GET.get("contest", "")
        self.org_query = []
        self.hide_organization_contests = False

        self.show_only_rated_contests = 0
        if self.GET_with_session(request, "show_only_rated_contests"):
            self.show_only_rated_contests = 1

    def get(self, request, *args, **kwargs):
        self.current_tab = "recommended"
        self.setup_contest_list(request)
        return super(ContestList, self).get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        key = "show_only_rated_contests"
        if key in request.GET:
            request.session[key] = request.GET.get(key) == "1"
        else:
            request.session[key] = False
        return HttpResponseRedirect(request.get_full_path())

    def get_queryset(self):
        queryset = self._recommended_contests_queryset
        if self.contest_query:
            queryset = queryset.filter(
                Q(key__icontains=self.contest_query)
                | Q(name__icontains=self.contest_query)
            )
        if self.show_only_rated_contests:
            queryset = queryset.filter(is_rated=True)
        return queryset

    def get_context_data(self, **kwargs):
        context = super(ContestList, self).get_context_data(**kwargs)
        context["page_type"] = "for_you"
        context["now"] = self._now
        context["first_page_href"] = "."
        context["contest_query"] = self.contest_query
        context["show_only_rated_contests"] = int(self.show_only_rated_contests)
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        Contest.prefetch_organization_ids(
            *[contest.id for contest in context["contests"]]
        )
        return context


class ContestProblemset(ContestMixin, TitleMixin, DetailView):
    template_name = "contest/problemset.html"

    def get_title(self):
        contest_name = self.object.name or ""
        return _("{contest_name} Problemset").format(contest_name=contest_name)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        contest = self.object

        # Superusers, editors and testers can always see the problemset
        if request.user.is_superuser or self.is_editor or self.is_tester:
            return super().get(request, *args, **kwargs)

        # If contest hasn't started yet (can_join is False when start_time > now), deny access
        if not contest.can_join:
            return generic_message(
                request,
                _("Problemset not available"),
                _(
                    "The contest has not started yet. Please wait until the contest begins."
                ),
            )

        # If contest has ended, allow access to everyone
        if contest.ended:
            return super().get(request, *args, **kwargs)

        # Contest is ongoing - check if user is currently in the contest with contest mode on
        # This properly handles windowed contests and respects the "In contest"/"Out contest" toggle
        is_in_this_contest = (
            getattr(request, "in_contest", False)
            and getattr(request, "participation", None) is not None
            and request.participation.contest == contest
        )
        if not is_in_this_contest:
            return generic_message(
                request,
                _("Problemset not available"),
                _("You must join the contest to view the problemset."),
            )

        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # Get all contest problems with their details
        # Filter out quiz-only entries (where problem_id is None)
        contest_problems = list(
            self.object.contest_problems.filter(problem_id__isnull=False)
            .select_related("problem", "problem__data_files")
            .order_by("order")
        )

        # Get contest problem IDs for prefetching
        contest_problem_ids = [cp.problem_id for cp in contest_problems]
        Problem.prefetch_cache_i18n_name(
            self.request.LANGUAGE_CODE, *contest_problem_ids
        )

        context["contest_problems"] = contest_problems
        context["problems"] = [cp.problem for cp in contest_problems]
        return context
