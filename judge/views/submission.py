import json
import os.path
from operator import attrgetter

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseBadRequest
from django.http import HttpResponseRedirect
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.defaultfilters import floatformat
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import escape
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views import View

from judge import event_poster as event
from judge.highlight_code import highlight_code
from judge.models import Contest, ContestParticipation
from judge.models import Language
from judge.models import Problem
from judge.models import ProblemTestCase
from judge.models import ProblemTranslation
from judge.models import Profile
from judge.models import Submission
from judge.utils.problems import get_result_data
from judge.utils.problems import user_completed_ids, user_editable_ids, user_tester_ids
from judge.utils.problem_data import get_problem_case
from judge.utils.raw_sql import join_sql_subquery, use_straight_join
from judge.utils.views import DiggPaginatorMixin
from judge.utils.infinite_paginator import InfinitePaginationMixin
from judge.utils.views import TitleMixin
from judge.utils.timedelta import nice_repr


def submission_related(queryset):
    return queryset.select_related("user__user", "problem", "language").only(
        "id",
        "user__user__username",
        "user__display_rank",
        "user__rating",
        "problem__name",
        "problem__code",
        "problem__is_public",
        "language__short_name",
        "language__key",
        "date",
        "time",
        "memory",
        "points",
        "result",
        "status",
        "case_points",
        "case_total",
        "current_testcase",
        "contest_object",
    )


class SubmissionMixin(object):
    model = Submission
    context_object_name = "submission"
    pk_url_kwarg = "submission"


class SubmissionDetailBase(LoginRequiredMixin, TitleMixin, SubmissionMixin, DetailView):
    def get_object(self, queryset=None):
        submission = super(SubmissionDetailBase, self).get_object(queryset)
        if submission.is_accessible_by(self.request.profile):
            return submission

        raise PermissionDenied()

    def get_title(self):
        submission = self.object
        return _("Submission of %(problem)s by %(user)s") % {
            "problem": submission.problem.translated_name(self.request.LANGUAGE_CODE),
            "user": submission.user.user.username,
        }

    def get_content_title(self):
        submission = self.object
        return mark_safe(
            escape(_("Submission of %(problem)s by %(user)s"))
            % {
                "problem": format_html(
                    '<a href="{0}">{1}</a>',
                    reverse("problem_detail", args=[submission.problem.code]),
                    submission.problem.translated_name(self.request.LANGUAGE_CODE),
                ),
                "user": format_html(
                    '<a href="{0}">{1}</a>',
                    reverse("user_page", args=[submission.user.user.username]),
                    submission.user.user.username,
                ),
            }
        )


class SubmissionSource(SubmissionDetailBase):
    template_name = "submission/source.html"

    def get_queryset(self):
        return super().get_queryset().select_related("source")

    def get_context_data(self, **kwargs):
        context = super(SubmissionSource, self).get_context_data(**kwargs)
        submission = self.object
        context["raw_source"] = submission.source.source.rstrip("\n")
        context["highlighted_source"] = highlight_code(
            submission.source.source, submission.language.pygments, linenos=False
        )
        return context


def get_hidden_subtasks(request, submission):
    contest = submission.contest_object
    if contest and contest.is_editable_by(request.user):
        return set()
    if contest and contest.format.has_hidden_subtasks:
        try:
            return contest.format.get_hidden_subtasks().get(
                str(submission.contest.problem.id), set()
            )
        except Exception:
            pass
    return set()


def make_batch(batch, cases, include_cases=True):
    result = {"id": batch}
    if include_cases:
        result["cases"] = cases
    if batch:
        result["points"] = sum(map(attrgetter("points"), cases))
        result["total"] = sum(map(attrgetter("total"), cases))
        result["AC"] = abs(result["points"] - result["total"]) < 1e-5

    return result


def group_test_cases(submission, hidden_subtasks, include_cases=True):
    cases = submission.test_cases.exclude(batch__in=hidden_subtasks)
    result = []
    buf = []
    last = None
    for case in cases:
        if case.batch != last and buf:
            result.append(make_batch(last, buf, include_cases))
            buf = []
        buf.append(case)
        last = case.batch
    if buf:
        result.append(make_batch(last, buf, include_cases))
    return result


def get_cases_data(submission):
    testcases = ProblemTestCase.objects.filter(dataset=submission.problem).order_by(
        "order"
    )

    if submission.is_pretested:
        testcases = testcases.filter(is_pretest=True)

    files = []
    for case in testcases:
        if case.input_file:
            files.append(case.input_file)
        if case.output_file:
            files.append(case.output_file)
    case_data = get_problem_case(submission.problem, files)

    problem_data = {}
    count = 0
    for case in testcases:
        if case.type != "C":
            continue
        count += 1
        problem_data[count] = {
            "input": case_data[case.input_file] if case.input_file else "",
            "answer": case_data[case.output_file] if case.output_file else "",
        }

    return problem_data


class SubmissionStatus(SubmissionDetailBase):
    template_name = "submission/status.html"

    def access_testcases_in_contest(self):
        contest = self.object.contest_or_none
        if contest is None:
            return False
        if contest.problem.problem.is_editable_by(self.request.user):
            return True
        if contest.problem.contest.is_in_contest(self.request.user):
            return False
        if contest.participation.ended:
            return True
        return False

    def get_context_data(self, **kwargs):
        context = super(SubmissionStatus, self).get_context_data(**kwargs)
        submission = self.object

        context["hidden_subtasks"] = get_hidden_subtasks(self.request, self.object)
        context["last_msg"] = event.last()
        context["batches"] = group_test_cases(
            submission, context["hidden_subtasks"], True
        )
        context["time_limit"] = submission.problem.time_limit
        context["can_see_testcases"] = False
        context["raw_source"] = submission.source.source.rstrip("\n")
        context["highlighted_source"] = highlight_code(
            submission.source.source, submission.language.pygments, linenos=False
        )

        contest = submission.contest_or_none
        show_testcases = False
        can_see_testcases = self.access_testcases_in_contest()

        if contest is not None:
            show_testcases = contest.problem.show_testcases or False

        if contest is None or show_testcases or can_see_testcases:
            context["cases_data"] = get_cases_data(submission)
            context["can_see_testcases"] = True
        try:
            lang_limit = submission.problem.language_limits.get(
                language=submission.language
            )
        except ObjectDoesNotExist:
            pass
        else:
            context["time_limit"] = lang_limit.time_limit
        return context


class SubmissionTestCaseQuery(SubmissionStatus):
    template_name = "submission/status-testcases.html"

    def get(self, request, *args, **kwargs):
        if "id" not in request.GET or not request.GET["id"].isdigit():
            return HttpResponseBadRequest()
        self.kwargs[self.pk_url_kwarg] = kwargs[self.pk_url_kwarg] = int(
            request.GET["id"]
        )
        return super(SubmissionTestCaseQuery, self).get(request, *args, **kwargs)


class SubmissionSourceRaw(SubmissionSource):
    def get(self, request, *args, **kwargs):
        submission = self.get_object()
        return HttpResponse(submission.source.source, content_type="text/plain")


@require_POST
def abort_submission(request, submission):
    submission = get_object_or_404(Submission, id=int(submission))
    # if (not request.user.is_authenticated or (submission.was_rejudged or (request.profile != submission.user)) and
    #         not request.user.has_perm('abort_any_submission')):
    #     raise PermissionDenied()
    if not request.user.is_authenticated or not request.user.has_perm(
        "abort_any_submission"
    ):
        raise PermissionDenied()
    submission.abort()
    return HttpResponseRedirect(reverse("submission_status", args=(submission.id,)))


class SubmissionsListBase(DiggPaginatorMixin, TitleMixin, ListView):
    model = Submission
    paginate_by = 50
    show_problem = True
    title = gettext_lazy("All submissions")
    content_title = gettext_lazy("All submissions")
    page_type = "all_submissions_list"
    template_name = "submission/list.html"
    context_object_name = "submissions"
    first_page_href = None
    include_frozen = False
    organization = None

    def get_result_data(self):
        result = self._get_result_data()
        for category in result["categories"]:
            category["name"] = _(category["name"])
        return result

    def _get_result_data(self):
        return get_result_data(self.get_queryset().order_by())

    def access_check(self, request):
        pass

    @cached_property
    def in_contest(self):
        return (
            self.request.user.is_authenticated
            and self.request.profile.current_contest is not None
            and self.request.in_contest_mode
        )

    @cached_property
    def contest(self):
        return self.request.profile.current_contest.contest

    def _get_entire_queryset(self):
        organization = self.organization or self.request.organization
        if organization:
            queryset = Submission.objects.filter(
                contest_object__organizations=organization
            )
        else:
            queryset = Submission.objects.all()
        use_straight_join(queryset)
        queryset = submission_related(queryset.order_by("-id"))
        if self.show_problem:
            queryset = queryset.prefetch_related(
                Prefetch(
                    "problem__translations",
                    queryset=ProblemTranslation.objects.filter(
                        language=self.request.LANGUAGE_CODE
                    ),
                    to_attr="_trans",
                )
            )
        if self.in_contest:
            queryset = queryset.filter(contest_object=self.contest)
            if not self.contest.can_see_full_scoreboard(self.request.user):
                queryset = queryset.filter(user=self.request.profile)
            if (
                self.contest.format.has_hidden_subtasks
                and not self.contest.is_editable_by(self.request.user)
            ):
                queryset = queryset.filter(user=self.request.profile)
            if self.contest.freeze_after and not self.include_frozen:
                queryset = queryset.exclude(
                    ~Q(user=self.request.profile),
                    date__gte=self.contest.freeze_after + self.contest.start_time,
                )
        else:
            queryset = queryset.select_related("contest_object").defer(
                "contest_object__description"
            )

            # This is not technically correct since contest organizers *should* see these, but
            # the join would be far too messy
            if not self.request.user.has_perm("judge.see_private_contest"):
                # Show submissions for any contest you can edit or visible scoreboard
                contest_queryset = Contest.objects.filter(
                    Q(authors=self.request.profile)
                    | Q(curators=self.request.profile)
                    | Q(scoreboard_visibility=Contest.SCOREBOARD_VISIBLE)
                    | Q(end_time__lt=timezone.now())
                ).distinct()
                queryset = queryset.filter(
                    Q(user=self.request.profile)
                    | Q(contest_object__in=contest_queryset)
                    | Q(contest_object__isnull=True)
                )

        if self.selected_languages:
            # Note (DMOJ): MariaDB can't optimize this subquery for some insane, unknown reason,
            # so we are forcing an eager evaluation to get the IDs right here.
            # Otherwise, with multiple language filters, MariaDB refuses to use an index
            # (or runs the subquery for every submission, which is even more horrifying to think about).
            queryset = queryset.filter(
                language__in=list(
                    Language.objects.filter(
                        key__in=self.selected_languages
                    ).values_list("id", flat=True)
                )
            )
        if self.selected_statuses:
            submission_results = [i for i, _ in Submission.RESULT]
            if self.selected_statuses[0] in submission_results:
                queryset = queryset.filter(result__in=self.selected_statuses)
            else:
                queryset = queryset.filter(status__in=self.selected_statuses)

        return queryset

    def get_queryset(self):
        queryset = self._get_entire_queryset()
        if not self.in_contest:
            join_sql_subquery(
                queryset,
                subquery=str(
                    Problem.get_visible_problems(self.request.user)
                    .distinct()
                    .only("id")
                    .query
                ),
                params=[],
                join_fields=[("problem_id", "id")],
                alias="visible_problems",
                related_model=Problem,
            )
        return queryset

    def get_my_submissions_page(self):
        return None

    def get_friend_submissions_page(self):
        return None

    def get_all_submissions_page(self):
        return reverse("all_submissions")

    def get_searchable_status_codes(self):
        all_statuses = list(Submission.RESULT)
        all_statuses.extend([i for i in Submission.STATUS if i not in all_statuses])
        hidden_codes = ["SC", "D", "G"]
        if not self.request.user.is_superuser and not self.request.user.is_staff:
            hidden_codes += ["IE"]
        return [(key, value) for key, value in all_statuses if key not in hidden_codes]

    def in_hidden_subtasks_contest(self):
        return (
            self.in_contest
            and self.contest.format.has_hidden_subtasks
            and not self.contest.is_editable_by(self.request.user)
        )

    def modify_attrs(self, submission):
        # Used to modify submission's info in contest with hidden subtasks
        batches = group_test_cases(
            submission, get_hidden_subtasks(self.request, submission), False
        )
        setattr(submission, "case_points", sum([i.get("points", 0) for i in batches]))
        setattr(submission, "batches", batches)
        if submission.status in ("IE", "CE", "AB"):
            setattr(submission, "_result_class", submission.result_class)
        else:
            setattr(submission, "_result_class", "TLE")

    def get_context_data(self, **kwargs):
        context = super(SubmissionsListBase, self).get_context_data(**kwargs)
        authenticated = self.request.user.is_authenticated
        context["dynamic_update"] = False
        context["show_problem"] = self.show_problem
        context["profile"] = self.request.profile
        context["all_languages"] = Language.objects.all().values_list("key", "name")
        context["selected_languages"] = self.selected_languages
        context["all_statuses"] = self.get_searchable_status_codes()
        context["selected_statuses"] = self.selected_statuses

        if not self.in_hidden_subtasks_contest():
            context["results_json"] = mark_safe(json.dumps(self.get_result_data()))
            context["results_colors_json"] = mark_safe(
                json.dumps(settings.DMOJ_STATS_SUBMISSION_RESULT_COLORS)
            )
        else:
            context["results_json"] = None

        context["page_suffix"] = suffix = (
            ("?" + self.request.GET.urlencode()) if self.request.GET else ""
        )
        context["first_page_href"] = (self.first_page_href or ".") + suffix
        context["my_submissions_link"] = self.get_my_submissions_page()
        context["friend_submissions_link"] = self.get_friend_submissions_page()
        context["all_submissions_link"] = self.get_all_submissions_page()
        context["page_type"] = self.page_type

        context["in_hidden_subtasks_contest"] = self.in_hidden_subtasks_contest()
        if context["in_hidden_subtasks_contest"]:
            for submission in context["submissions"]:
                self.modify_attrs(submission)
        return context

    def get(self, request, *args, **kwargs):
        check = self.access_check(request)
        if check is not None:
            return check

        self.selected_languages = request.GET.getlist("language")
        self.selected_statuses = request.GET.getlist("status")

        if self.in_contest and self.contest.is_editable_by(self.request.user):
            self.include_frozen = True

        if "results" in request.GET:
            return JsonResponse(self.get_result_data())

        return super(SubmissionsListBase, self).get(request, *args, **kwargs)


class UserMixin(object):
    def get(self, request, *args, **kwargs):
        if "user" not in kwargs and "participation" not in kwargs:
            raise ImproperlyConfigured("Must pass a user or participation")
        if "user" in kwargs:
            self.profile = get_object_or_404(Profile, user__username=kwargs["user"])
            self.username = kwargs["user"]
        else:
            self.participation = get_object_or_404(
                ContestParticipation, id=kwargs["participation"]
            )
            self.profile = self.participation.user
            self.username = self.profile.user.username
        if self.profile == request.profile:
            self.include_frozen = True
        return super(UserMixin, self).get(request, *args, **kwargs)


class ConditionalUserTabMixin(object):
    def get_context_data(self, **kwargs):
        context = super(ConditionalUserTabMixin, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated and self.request.profile == self.profile:
            context["page_type"] = "my_submissions_tab"
        else:
            context["page_type"] = "user_submissions_tab"
            context["tab_username"] = self.profile.user.username
        return context


class GeneralSubmissions(SubmissionsListBase):
    def get_my_submissions_page(self):
        if self.request.user.is_authenticated:
            return reverse(
                "all_user_submissions", kwargs={"user": self.request.user.username}
            )
        return None

    def get_friend_submissions_page(self):
        if self.request.user.is_authenticated:
            return reverse("all_friend_submissions")
        return None


class AllUserSubmissions(ConditionalUserTabMixin, UserMixin, GeneralSubmissions):
    def get_queryset(self):
        return (
            super(AllUserSubmissions, self)
            .get_queryset()
            .filter(user_id=self.profile.id)
        )

    def get_title(self):
        if self.request.user.is_authenticated and self.request.profile == self.profile:
            return _("All my submissions")
        return _("All submissions by %s") % self.username

    def get_content_title(self):
        if self.request.user.is_authenticated and self.request.profile == self.profile:
            return format_html(_("All my submissions"))
        return format_html(
            _('All submissions by <a href="{1}">{0}</a>'),
            self.username,
            reverse("user_page", args=[self.username]),
        )

    def get_context_data(self, **kwargs):
        context = super(AllUserSubmissions, self).get_context_data(**kwargs)
        context["dynamic_update"] = context["page_obj"].number == 1
        context["dynamic_user_id"] = self.profile.id
        context["last_msg"] = event.last()
        return context


class AllFriendSubmissions(LoginRequiredMixin, GeneralSubmissions):
    def get_queryset(self):
        friends = self.request.profile.get_friends()
        return (
            super(AllFriendSubmissions, self).get_queryset().filter(user_id__in=friends)
        )

    def get_title(self):
        return _("All friend submissions")

    def get_context_data(self, **kwargs):
        context = super(AllFriendSubmissions, self).get_context_data(**kwargs)
        context["dynamic_update"] = False
        context["page_type"] = "friend_tab"
        return context


class ProblemSubmissionsBase(SubmissionsListBase):
    show_problem = False
    dynamic_update = True
    check_contest_in_access_check = False

    def get_queryset(self):
        if (
            self.in_contest
            and not self.contest.contest_problems.filter(
                problem_id=self.problem.id
            ).exists()
        ):
            raise Http404()
        return (
            super(ProblemSubmissionsBase, self)
            ._get_entire_queryset()
            .filter(problem_id=self.problem.id)
        )

    def get_title(self):
        return _("All submissions for %s") % self.problem_name

    def get_content_title(self):
        return format_html(
            'All submissions for <a href="{1}">{0}</a>',
            self.problem_name,
            reverse("problem_detail", args=[self.problem.code]),
        )

    def access_check_contest(self, request):
        if self.in_contest:
            if not self.contest.can_see_own_scoreboard(request.user):
                raise Http404()
            if not self.contest.is_accessible_by(request.user):
                raise Http404()

    def access_check(self, request):
        if self.check_contest_in_access_check:
            self.access_check_contest(request)
        else:
            is_own = hasattr(self, "is_own") and self.is_own
            if not is_own and not self.problem.is_accessible_by(
                request.user, request.in_contest_mode
            ):
                raise Http404()

    def get(self, request, *args, **kwargs):
        if "problem" not in kwargs:
            raise ImproperlyConfigured(_("Must pass a problem"))
        self.problem = get_object_or_404(Problem, code=kwargs["problem"])
        self.problem_name = self.problem.translated_name(self.request.LANGUAGE_CODE)
        return super(ProblemSubmissionsBase, self).get(request, *args, **kwargs)

    def get_all_submissions_page(self):
        return reverse(
            "chronological_submissions", kwargs={"problem": self.problem.code}
        )

    def get_context_data(self, **kwargs):
        context = super(ProblemSubmissionsBase, self).get_context_data(**kwargs)
        if self.dynamic_update:
            context["dynamic_update"] = context["page_obj"].number == 1
            context["dynamic_problem_id"] = self.problem.id
            context["last_msg"] = event.last()
        context["best_submissions_link"] = reverse(
            "ranked_submissions", kwargs={"problem": self.problem.code}
        )
        return context


class ProblemSubmissions(ProblemSubmissionsBase):
    def get_my_submissions_page(self):
        if self.request.user.is_authenticated:
            return reverse(
                "user_submissions",
                kwargs={
                    "problem": self.problem.code,
                    "user": self.request.user.username,
                },
            )


class UserProblemSubmissions(ConditionalUserTabMixin, UserMixin, ProblemSubmissions):
    check_contest_in_access_check = False

    @cached_property
    def is_own(self):
        return (
            self.request.user.is_authenticated and self.request.profile == self.profile
        )

    def access_check(self, request):
        super(UserProblemSubmissions, self).access_check(request)

        if not self.is_own:
            self.access_check_contest(request)

    def get_queryset(self):
        return (
            super(UserProblemSubmissions, self)
            .get_queryset()
            .filter(user_id=self.profile.id)
        )

    def get_title(self):
        if self.is_own:
            return _("My submissions for %(problem)s") % {"problem": self.problem_name}
        return _("%(user)s's submissions for %(problem)s") % {
            "user": self.username,
            "problem": self.problem_name,
        }

    def get_content_title(self):
        if self.request.user.is_authenticated and self.request.profile == self.profile:
            return format_html(
                """My submissions for <a href="{3}">{2}</a>""",
                self.username,
                reverse("user_page", args=[self.username]),
                self.problem_name,
                reverse("problem_detail", args=[self.problem.code]),
            )
        return format_html(
            """<a href="{1}">{0}</a>'s submissions for <a href="{3}">{2}</a>""",
            self.username,
            reverse("user_page", args=[self.username]),
            self.problem_name,
            reverse("problem_detail", args=[self.problem.code]),
        )

    def get_context_data(self, **kwargs):
        context = super(UserProblemSubmissions, self).get_context_data(**kwargs)
        context["dynamic_user_id"] = self.profile.id
        return context


def single_submission(request, submission_id, show_problem=True):
    request.no_profile_update = True
    authenticated = request.user.is_authenticated
    submission = get_object_or_404(
        submission_related(Submission.objects.all()), id=int(submission_id)
    )

    if not submission.problem.is_accessible_by(request.user):
        raise Http404()

    return render(
        request,
        "submission/row.html",
        {
            "submission": submission,
            "show_problem": show_problem,
            "problem_name": show_problem
            and submission.problem.translated_name(request.LANGUAGE_CODE),
            "profile": request.profile if authenticated else None,
        },
    )


def single_submission_query(request):
    request.no_profile_update = True
    if "id" not in request.GET or not request.GET["id"].isdigit():
        return HttpResponseBadRequest()
    try:
        show_problem = int(request.GET.get("show_problem", "1"))
    except ValueError:
        return HttpResponseBadRequest()
    return single_submission(request, int(request.GET["id"]), bool(show_problem))


class AllSubmissions(InfinitePaginationMixin, GeneralSubmissions):
    stats_update_interval = 3600

    @property
    def use_infinite_pagination(self):
        return not self.in_contest

    def get_context_data(self, **kwargs):
        context = super(AllSubmissions, self).get_context_data(**kwargs)
        context["dynamic_update"] = (
            context["page_obj"].number == 1
        ) and not self.request.organization
        context["last_msg"] = event.last()
        context["stats_update_interval"] = self.stats_update_interval
        return context

    def _get_result_data(self):
        if self.request.organization or self.in_contest:
            return super(AllSubmissions, self)._get_result_data()

        key = "global_submission_result_data"
        if self.selected_statuses:
            key += ":" + ",".join(self.selected_statuses)
        if self.selected_languages:
            key += ":" + ",".join(self.selected_languages)
        result = cache.get(key)
        if result:
            return result
        queryset = Submission.objects
        if self.selected_languages:
            queryset = queryset.filter(
                language__in=Language.objects.filter(key__in=self.selected_languages)
            )
        if self.selected_statuses:
            submission_results = [i for i, _ in Submission.RESULT]
            if self.selected_statuses[0] in submission_results:
                queryset = queryset.filter(result__in=self.selected_statuses)
            else:
                queryset = queryset.filter(status__in=self.selected_statuses)
        result = get_result_data(queryset)
        cache.set(key, result, self.stats_update_interval)
        return result


class ForceContestMixin(object):
    @property
    def in_contest(self):
        return True

    @property
    def contest(self):
        return self._contest

    def access_check(self, request):
        super(ForceContestMixin, self).access_check(request)

        if not request.user.has_perm("judge.see_private_contest"):
            if not self.contest.is_visible:
                raise Http404()
            if (
                self.contest.start_time is not None
                and self.contest.start_time > timezone.now()
            ):
                raise Http404()

    def get_problem_number(self, problem):
        return (
            self.contest.contest_problems.select_related("problem")
            .get(problem=problem)
            .order
        )

    def get(self, request, *args, **kwargs):
        if "contest" not in kwargs:
            raise ImproperlyConfigured(_("Must pass a contest"))
        self._contest = get_object_or_404(Contest, key=kwargs["contest"])
        return super(ForceContestMixin, self).get(request, *args, **kwargs)


class UserContestSubmissions(ForceContestMixin, UserProblemSubmissions):
    check_contest_in_access_check = True

    def get_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return "%s's submissions for %s in %s" % (
                self.username,
                self.problem_name,
                self.contest.name,
            )
        return "%s's submissions for problem %s in %s" % (
            self.username,
            self.get_problem_number(self.problem),
            self.contest.name,
        )

    def access_check(self, request):
        super(UserContestSubmissions, self).access_check(request)
        if not self.contest.users.filter(user_id=self.profile.id).exists():
            raise Http404()

    def get_content_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return format_html(
                _(
                    '<a href="{1}">{0}</a>\'s submissions for '
                    '<a href="{3}">{2}</a> in <a href="{5}">{4}</a>'
                ),
                self.username,
                reverse("user_page", args=[self.username]),
                self.problem_name,
                reverse("problem_detail", args=[self.problem.code]),
                self.contest.name,
                reverse("contest_view", args=[self.contest.key]),
            )
        return format_html(
            _(
                '<a href="{1}">{0}</a>\'s submissions for '
                'problem {2} in <a href="{4}">{3}</a>'
            ),
            self.username,
            reverse("user_page", args=[self.username]),
            self.get_problem_number(self.problem),
            self.contest.name,
            reverse("contest_view", args=[self.contest.key]),
        )


class UserContestSubmissionsAjax(UserContestSubmissions):
    template_name = "submission/user-ajax.html"

    def contest_time(self, s):
        if s.contest.participation.live:
            if self.contest.time_limit:
                return s.date - s.contest.participation.real_start
            return s.date - self.contest.start_time
        return None

    def get_best_subtask_points(self):
        if self.contest.format.has_hidden_subtasks:
            contest_problem = self.contest.contest_problems.get(problem=self.problem)
            best_subtasks = {}
            total_points = 0
            problem_points = 0
            achieved_points = 0
            hidden_subtasks = self.contest.format.get_hidden_subtasks()

            for (
                problem_id,
                pp,
                time,
                subtask_points,
                total_subtask_points,
                subtask,
                sub_id,
            ) in self.contest.format.get_results_by_subtask(
                self.participation, self.include_frozen
            ):
                if contest_problem.id != problem_id or total_subtask_points == 0:
                    continue
                if not subtask:
                    subtask = 0
                problem_points = pp
                submission = Submission.objects.get(id=sub_id)
                if subtask in hidden_subtasks.get(
                    str(problem_id), set()
                ) and not self.contest.is_editable_by(self.request.user):
                    best_subtasks[subtask] = {
                        "submission": None,
                        "contest_time": None,
                        "points": "???",
                        "total": total_subtask_points,
                    }
                else:
                    best_subtasks[subtask] = {
                        "submission": submission,
                        "contest_time": nice_repr(
                            self.contest_time(submission), "noday"
                        ),
                        "points": subtask_points,
                        "total": total_subtask_points,
                    }
                    achieved_points += subtask_points
                total_points += total_subtask_points
            for subtask in best_subtasks.values():
                if subtask["points"] != "???":
                    subtask["points"] = floatformat(
                        subtask["points"] / total_points * problem_points,
                        -self.contest.points_precision,
                    )
                subtask["total"] = floatformat(
                    subtask["total"] / total_points * problem_points,
                    -self.contest.points_precision,
                )
            if total_points > 0 and best_subtasks:
                achieved_points = achieved_points / total_points * problem_points
                return best_subtasks, achieved_points, problem_points
        return None

    def get_context_data(self, **kwargs):
        context = super(UserContestSubmissionsAjax, self).get_context_data(**kwargs)
        context["contest"] = self.contest
        context["problem"] = self.problem
        context["profile"] = self.profile

        contest_problem = self.contest.contest_problems.get(problem=self.problem)
        filtered_submissions = []

        # Only show this for some users when using ioi16
        if not self.contest.format.has_hidden_subtasks or self.contest.is_editable_by(
            self.request.user
        ):
            for s in context["submissions"]:
                if not hasattr(s, "contest"):
                    continue
                contest_time = self.contest_time(s)
                if contest_time:
                    s.contest_time = nice_repr(contest_time, "noday")
                else:
                    s.contest_time = None
                total = floatformat(
                    contest_problem.points, -self.contest.points_precision
                )
                points = floatformat(s.contest.points, -self.contest.points_precision)
                s.display_point = f"{points} / {total}"
                filtered_submissions.append(s)
            context["submissions"] = filtered_submissions
        else:
            context["submissions"] = None

        best_subtasks = self.get_best_subtask_points()
        if best_subtasks:
            (
                context["best_subtasks"],
                context["points"],
                context["total"],
            ) = best_subtasks
            if context["points"] != "???":
                context["points"] = floatformat(
                    context["points"], -self.contest.points_precision
                )
            context["total"] = floatformat(
                context["total"], -self.contest.points_precision
            )
            context["subtasks"] = sorted(context["best_subtasks"].keys())
        return context

    def get(self, request, *args, **kwargs):
        try:
            return super(UserContestSubmissionsAjax, self).get(request, *args, **kwargs)
        except Http404:
            return HttpResponse(_("You don't have permission to access."))


class SubmissionSourceFileView(View):
    def get(self, request, filename):
        filepath = os.path.join(settings.DMOJ_SUBMISSION_ROOT, filename)
        if not os.path.exists(filepath):
            raise Http404("File not found")
        response = HttpResponse()
        with open(filepath, "rb") as f:
            response.content = f.read()
        response["Content-Type"] = "application/octet-stream"
        response["Content-Disposition"] = "attachment; filename=%s" % (filename,)
        return response
