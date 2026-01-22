import logging
import os
from operator import itemgetter
from random import randrange
from copy import deepcopy

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Prefetch, Q
from django.db.utils import ProgrammingError
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render
from django.template.loader import get_template
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.functional import cached_property
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import ListView, View, CreateView
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from django.views.generic.edit import FormMixin

from judge.views.comment import CommentableMixin
from judge.forms import (
    ProblemCloneForm,
    ProblemSubmitForm,
    ProblemPointsVoteForm,
    ProblemEditForm,
    ProblemAddForm,
    LanguageLimitEditForm,
    LanguageTemplateEditForm,
    ProblemSolutionEditForm,
    ProblemTranslationEditForm,
)
from judge.models import (
    ContestProblem,
    ContestSubmission,
    Judge,
    Language,
    LanguageLimit,
    LanguageTemplate,
    Problem,
    ContestProblemClarification,
    ProblemGroup,
    ProblemTranslation,
    ProblemType,
    RuntimeVersion,
    Solution,
    Submission,
    SubmissionSource,
    Profile,
    Contest,
    Quiz,
)
from judge.pdf_problems import DefaultPdfMaker, HAS_PDF
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.opengraph import generate_opengraph
from judge.utils.problems import (
    contest_attempted_ids,
    contest_completed_ids,
    hot_problems,
    user_attempted_ids,
    user_completed_ids,
    get_related_problems,
    get_user_recommended_problems,
    RecommendationType,
)
from judge.utils.strings import safe_float_or_none, safe_int_or_none
from judge.utils.tickets import own_ticket_filter
from judge.utils.views import (
    QueryStringSortMixin,
    SingleObjectFormView,
    TitleMixin,
    generic_message,
)
from judge.ml.collab_filter import CollabFilter
from judge.views.pagevote import PageVoteDetailView
from judge.views.bookmark import BookMarkDetailView
from judge.views.feed import FeedView
from judge.models.problem import (
    get_distinct_problem_points,
    get_all_problem_types,
    get_all_problem_groups,
)
from judge.models.runtime import get_all_languages

from judge.tasks import rescore_problem
from judge.tasks.llm import (
    generate_solution_task,
    improve_markdown_task,
    tag_problem_task,
)

from problem_tag import get_problem_tag_service


def get_contest_problem(problem, profile):
    try:
        return problem.contests.get(contest_id=profile.current_contest.contest_id)
    except ObjectDoesNotExist:
        return None


def get_contest_submission_count(problem, profile, virtual):
    return (
        profile.current_contest.submissions.exclude(submission__status__in=["IE"])
        .filter(problem__problem=problem, participation__virtual=virtual)
        .count()
    )


def get_problems_in_organization(request, organization):
    problem_list = ProblemList(request=request)
    problem_list.setup_problem_list(request)
    problem_list.org_query = [organization.id]
    problems = problem_list.get_normal_queryset()
    return problems


class ProblemMixin(object):
    model = Problem
    slug_url_kwarg = "problem"
    slug_field = "code"

    def get_object(self, queryset=None):
        problem = super(ProblemMixin, self).get_object(queryset)
        if not problem.is_accessible_by(self.request.user):
            raise Http404()
        return problem

    def no_such_problem(self):
        code = self.kwargs.get(self.slug_url_kwarg, None)
        return generic_message(
            self.request,
            _("No such problem"),
            _('Could not find a problem with the code "%s".') % code,
            status=404,
        )

    def get(self, request, *args, **kwargs):
        try:
            return super(ProblemMixin, self).get(request, *args, **kwargs)
        except Http404 as e:
            return self.no_such_problem()


class SolvedProblemMixin(object):
    def get_completed_problems(self):
        if self.in_contest:
            return contest_completed_ids(self.profile.current_contest)
        else:
            return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_attempted_problems(self):
        if self.in_contest:
            return contest_attempted_ids(self.profile.current_contest)
        else:
            return user_attempted_ids(self.profile) if self.profile is not None else ()

    def get_latest_attempted_problems(self, limit=None, queryset_ids=None):
        if self.in_contest or not self.profile:
            return ()
        result = user_attempted_ids(self.profile)
        if queryset_ids:
            result = {i: v for i, v in result.items() if i in queryset_ids}
        result = list(result.values())
        result = sorted(result, key=lambda d: -d["last_submission"])
        if limit:
            result = result[:limit]
        return result

    @cached_property
    def in_contest(self):
        return (
            self.profile is not None
            and self.profile.current_contest is not None
            and self.request.in_contest_mode
        )

    @cached_property
    def contest(self):
        return self.request.profile.current_contest.contest

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile


class ProblemSolution(
    SolvedProblemMixin,
    ProblemMixin,
    TitleMixin,
    CommentableMixin,
    PageVoteDetailView,
    BookMarkDetailView,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    context_object_name = "solution"
    template_name = "problem/editorial.html"

    def get_title(self):
        return _("Editorial for {0}").format(self.problem.name)

    def get_content_title(self):
        return format_html(
            _('Editorial for <a href="{1}">{0}</a>'),
            self.problem.name,
            reverse("problem_detail", args=[self.problem.code]),
        )

    def get_object(self):
        self.problem = super().get_object()
        solution = get_object_or_404(Solution, problem=self.problem)
        return solution

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(
            self.get_context_data(
                object=self.object,
            )
        )

    def get_context_data(self, **kwargs):
        context = super(ProblemSolution, self).get_context_data(**kwargs)
        solution = self.object
        if (
            not solution.is_public or solution.publish_on > timezone.now()
        ) and not self.request.user.has_perm("judge.see_private_solution"):
            raise Http404()

        context["problem"] = self.problem
        context["has_solved_problem"] = self.problem.id in self.get_completed_problems()
        context["can_edit_problem"] = self.problem.is_editable_by(self.request.user)

        # Add comment context
        context = self.get_comment_context(context)

        return context


class ProblemRaw(
    ProblemMixin, TitleMixin, TemplateResponseMixin, SingleObjectMixin, View
):
    context_object_name = "problem"
    template_name = "problem/raw.html"

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(ProblemRaw, self).get_context_data(**kwargs)
        user = self.request.user
        authed = user.is_authenticated
        context["problem_name"] = self.object.name
        contest_problem = (
            None
            if not authed or user.profile.current_contest is None
            else get_contest_problem(self.object, user.profile)
        )
        context["contest_problem"] = contest_problem
        context["url"] = self.request.build_absolute_uri()
        context["description"] = self.object.description
        if hasattr(self.object, "data_files"):
            context["fileio_input"] = self.object.data_files.fileio_input
            context["fileio_output"] = self.object.data_files.fileio_output
        else:
            context["fileio_input"] = None
            context["fileio_output"] = None
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        with translation.override(settings.LANGUAGE_CODE):
            return self.render_to_response(
                self.get_context_data(
                    object=self.object,
                )
            )


class ProblemDetail(
    ProblemMixin,
    SolvedProblemMixin,
    CommentableMixin,
    PageVoteDetailView,
    BookMarkDetailView,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    context_object_name = "problem"
    template_name = "problem/problem.html"

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(
            self.get_context_data(
                object=self.object,
            )
        )

    def get_context_data(self, **kwargs):
        context = super(ProblemDetail, self).get_context_data(**kwargs)
        user = self.request.user
        authed = user.is_authenticated
        context["has_submissions"] = (
            authed
            and Submission.objects.filter(
                user=user.profile, problem=self.object
            ).exists()
        )

        # Make sure we pass the object to the template
        context["object"] = self.object
        contest_problem = (
            None
            if not authed or user.profile.current_contest is None
            else get_contest_problem(self.object, user.profile)
        )
        context["contest_problem"] = contest_problem

        if contest_problem:
            clarifications = contest_problem.clarifications
            context["has_clarifications"] = clarifications.count() > 0
            context["clarifications"] = clarifications.order_by("-date")
            context["submission_limit"] = contest_problem.max_submissions
            if contest_problem.max_submissions:
                context["submissions_left"] = max(
                    contest_problem.max_submissions
                    - get_contest_submission_count(
                        self.object, user.profile, user.profile.current_contest.virtual
                    ),
                    0,
                )

        context["available_judges"] = Judge.objects.filter(
            online=True, problems=self.object
        )
        context["show_languages"] = len(self.object.get_allowed_languages()) != len(
            get_all_languages()
        )
        context["has_pdf_render"] = HAS_PDF
        context["completed_problem_ids"] = self.get_completed_problems()
        context["attempted_problems"] = self.get_attempted_problems()

        can_edit = self.object.is_editable_by(user)
        context["can_edit_problem"] = can_edit
        if user.is_authenticated:
            tickets = self.object.tickets
            if not can_edit:
                tickets = tickets.filter(own_ticket_filter(user.profile.id))
            tickets = list(tickets.values("id", "is_open"))

            context["has_tickets"] = len(tickets) > 0
            context["num_open_tickets"] = len([t for t in tickets if t["is_open"]])

        context["title"] = self.object.translated_name(self.request.LANGUAGE_CODE)
        context["language"] = self.request.LANGUAGE_CODE
        context["description"] = self.object.translated_description(
            self.request.LANGUAGE_CODE
        )

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph(
                "generated-meta-problem:%s:%d" % (context["language"], self.object.id),
                context["description"],
            )
        context["meta_description"] = self.object.summary or metadata[0]
        context["og_image"] = self.object.og_image or metadata[1]
        if hasattr(self.object, "data_files"):
            context["fileio_input"] = self.object.data_files.fileio_input
            context["fileio_output"] = self.object.data_files.fileio_output
        else:
            context["fileio_input"] = None
            context["fileio_output"] = None
        if not self.in_contest and settings.ML_OUTPUT_PATH:
            context["related_problems"] = get_related_problems(
                self.profile, self.object
            )

        # Add comment context
        context = self.get_comment_context(context)

        return context


class LatexError(Exception):
    pass


class ProblemPdfView(ProblemMixin, SingleObjectMixin, View):
    logger = logging.getLogger("judge.problem.pdf")
    languages = set(map(itemgetter(0), settings.LANGUAGES))

    def get(self, request, *args, **kwargs):
        if not HAS_PDF:
            raise Http404()

        language = kwargs.get("language", self.request.LANGUAGE_CODE)
        if language not in self.languages:
            raise Http404()

        problem = self.get_object()
        try:
            trans = problem.translations.get(language=language)
        except ProblemTranslation.DoesNotExist:
            trans = None

        # Use default_storage for PDF cache (works with S3 and local)
        cache_path = "pdf_cache/%s.%s.pdf" % (problem.code, language)

        if not default_storage.exists(cache_path):
            self.logger.info("Rendering: %s.%s.pdf", problem.code, language)
            with DefaultPdfMaker() as maker, translation.override(language):
                problem_name = problem.name if trans is None else trans.name
                maker.html = (
                    get_template("problem/raw.html")
                    .render(
                        {
                            "problem": problem,
                            "problem_name": problem_name,
                            "description": (
                                problem.description
                                if trans is None
                                else trans.description
                            ),
                            "url": request.build_absolute_uri(),
                        }
                    )
                    .replace('"//', '"https://')
                    .replace("'//", "'https://")
                )
                maker.title = problem_name
                assets = ["style.css"]
                for file in assets:
                    maker.load(file, os.path.join(settings.DMOJ_RESOURCES, file))
                maker.make()
                if not maker.success:
                    self.logger.error("Failed to render PDF for %s", problem.code)
                    return HttpResponse(
                        maker.log, status=500, content_type="text/plain"
                    )
                # Save rendered PDF to storage
                with open(maker.pdffile, "rb") as f:
                    default_storage.save(cache_path, ContentFile(f.read()))

        response = HttpResponse()
        with default_storage.open(cache_path, "rb") as f:
            response.content = f.read()

        response["Content-Type"] = "application/pdf"
        response["Content-Disposition"] = "inline; filename=%s.%s.pdf" % (
            problem.code,
            language,
        )
        return response


class ProblemPdfDescriptionView(ProblemMixin, SingleObjectMixin, View):
    def get(self, request, *args, **kwargs):
        problem = self.get_object()
        if not problem.pdf_description:
            raise Http404()
        response = HttpResponse()
        # Use FileField's storage-aware open() method (works with S3 and local)
        with problem.pdf_description.open("rb") as f:
            response.content = f.read()

        response["Content-Type"] = "application/pdf"
        response["Content-Disposition"] = "inline; filename=%s.pdf" % (problem.code,)
        return response


class ProblemList(QueryStringSortMixin, TitleMixin, SolvedProblemMixin, ListView):
    model = Problem
    title = gettext_lazy("Problems")
    context_object_name = "problems"
    template_name = "problem/list.html"
    paginate_by = 50
    sql_sort = frozenset(("date", "points", "ac_rate", "user_count", "code"))
    manual_sort = frozenset(("name", "group", "solved"))
    all_sorts = sql_sort | manual_sort
    default_desc = frozenset(("date", "points", "ac_rate", "user_count"))

    def get_default_sort_order(self, request):
        if "search" in request.GET and settings.ENABLE_FTS:
            return "-relevance"
        return "-date"

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        paginator = DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            count=queryset.values("pk").count() if not self.in_contest else None,
            **kwargs,
        )
        if not self.in_contest:
            queryset = self.sort_queryset(queryset)
            paginator.object_list = queryset
        return paginator

    def sort_queryset(self, queryset):
        sort_key = self.order.lstrip("-")
        if sort_key in self.sql_sort:
            queryset = queryset.order_by(self.order)
        elif sort_key == "name":
            queryset = queryset.order_by(self.order)
        elif sort_key == "group":
            queryset = queryset.order_by(self.order + "__name")
        elif sort_key == "solved":
            if self.request.user.is_authenticated:
                profile = self.request.profile
                solved = user_completed_ids(profile)
                attempted = user_attempted_ids(profile)

                def _solved_sort_order(problem_id):
                    if problem_id in solved:
                        return 1
                    if problem_id in attempted:
                        return 0
                    return -1

                queryset = list(queryset)
                queryset.sort(
                    key=_solved_sort_order, reverse=self.order.startswith("-")
                )

        return queryset

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated:
            return None
        return self.request.profile

    def get_contest_queryset(self):
        # Filter out quiz-only entries (where problem_id is None)
        return (
            self.profile.current_contest.contest.contest_problems.filter(
                problem_id__isnull=False
            )
            .order_by("order")
            .values_list("problem_id", flat=True)
        )

    def get_org_query(self, query):
        if not self.profile:
            return []
        return [
            i
            for i in query
            if i in self.profile.organizations.values_list("id", flat=True)
        ][:3]

    def get_normal_queryset(self):
        queryset = Problem.get_visible_problems(self.request.user)
        if self.profile is not None and self.hide_solved:
            solved_problems = self.get_completed_problems()
            queryset = queryset.exclude(id__in=solved_problems)
        if not self.org_query and self.request.organization:
            self.org_query = [self.request.organization.id]
        if self.org_query:
            self.org_query = self.get_org_query(self.org_query)
            contest_problems = (
                Contest.objects.filter(organizations__in=self.org_query)
                .select_related("problems")
                .values_list("contest_problems__problem__id")
                .distinct()
            )
            queryset = queryset.filter(
                Q(organizations__in=self.org_query) | Q(id__in=contest_problems)
            )
        if self.author_query:
            queryset = queryset.filter(authors__in=self.author_query)
        if self.show_types:
            queryset = queryset.prefetch_related("types")
        if self.category is not None:
            queryset = queryset.filter(group__id=self.category)
        if self.selected_types:
            queryset = queryset.filter(types__in=self.selected_types)
        if "search" in self.request.GET:
            self.search_query = query = " ".join(
                self.request.GET.getlist("search")
            ).strip()
            if query:
                substr_queryset = queryset.filter(
                    Q(code__icontains=query)
                    | Q(name__icontains=query)
                    | Q(
                        translations__name__icontains=query,
                        translations__language=self.request.LANGUAGE_CODE,
                    )
                )
                if settings.ENABLE_FTS:
                    queryset = (
                        queryset.search(query, queryset.BOOLEAN).extra(
                            order_by=["-relevance"]
                        )
                        | substr_queryset
                    )
                else:
                    queryset = substr_queryset
        if self.point_start is not None:
            queryset = queryset.filter(points__gte=self.point_start)
        if self.point_end is not None:
            queryset = queryset.filter(points__lte=self.point_end)

        return queryset.distinct().values_list("id", flat=True)

    def get_queryset(self):
        if self.in_contest:
            return self.get_contest_queryset()
        else:
            return self.get_normal_queryset()

    def get_context_data(self, **kwargs):
        context = super(ProblemList, self).get_context_data(**kwargs)

        context["hide_solved"] = 0 if self.in_contest else int(self.hide_solved)
        context["show_types"] = 0 if self.in_contest else int(self.show_types)
        context["full_text"] = 0 if self.in_contest else int(self.full_text)
        context["show_editorial"] = 0 if self.in_contest else int(self.show_editorial)
        context["show_solved_only"] = (
            0 if self.in_contest else int(self.show_solved_only)
        )

        if self.request.profile:
            context["organizations"] = self.request.profile.get_organizations()
        context["category"] = self.category
        context["categories"] = get_all_problem_groups()
        if self.show_types:
            context["selected_types"] = self.selected_types
            context["problem_types"] = get_all_problem_types()
        context["has_fts"] = settings.ENABLE_FTS
        context["org_query"] = self.org_query
        context["author_query"] = Profile.objects.filter(id__in=self.author_query)
        context["search_query"] = self.search_query
        context["completed_problem_ids"] = self.get_completed_problems()
        context["attempted_problems"] = self.get_attempted_problems()
        if self.org_query:
            context["last_attempted_problems"] = self.get_latest_attempted_problems(
                15, context["problems"]
            )
        context["page_type"] = "list"
        context.update(self.get_sort_paginate_context())
        if not self.in_contest:
            context.update(self.get_sort_context())
            (
                context["point_start"],
                context["point_end"],
                context["point_values"],
            ) = self.get_noui_slider_points()
        else:
            context["point_start"], context["point_end"], context["point_values"] = (
                0,
                0,
                {},
            )
            context["hide_contest_scoreboard"] = self.contest.scoreboard_visibility in (
                self.contest.SCOREBOARD_AFTER_CONTEST,
                self.contest.SCOREBOARD_AFTER_PARTICIPATION,
            )
            context["has_clarifications"] = False

            participation = self.request.profile.current_contest
            clarifications = ContestProblemClarification.objects.filter(
                problem__in=participation.contest.contest_problems.all()
            )
            context["has_clarifications"] = clarifications.count() > 0
            context["clarifications"] = clarifications.order_by("-date")
            context["current_contest"] = participation.contest
            if participation.contest.is_editable_by(self.request.user):
                context["can_edit_contest"] = True

            # Get contest quizzes
            contest_quiz_entries = (
                participation.contest.contest_problems.filter(quiz__isnull=False)
                .select_related("quiz")
                .order_by("order")
            )
            context["contest_quizzes"] = contest_quiz_entries
            context["has_contest_quizzes"] = contest_quiz_entries.exists()

        context["has_show_editorial_option"] = True
        context["show_contest_mode"] = self.request.in_contest_mode

        Problem.prefetch_cache_has_public_editorial(*context["problems"])
        if context["show_types"]:
            Problem.prefetch_cache_types_name(*context["problems"])

        Problem.prefetch_cache_i18n_name(
            self.request.LANGUAGE_CODE, *context["problems"]
        )

        context["problems"] = Problem.get_cached_instances(*context["problems"])

        return context

    def get_noui_slider_points(self):
        points = get_distinct_problem_points()
        if not points:
            return 0, 0, {}
        if len(points) == 1:
            return (
                points[0],
                points[0],
                {
                    "min": points[0] - 1,
                    "max": points[0] + 1,
                },
            )

        start, end = points[0], points[-1]
        if self.point_start is not None:
            start = self.point_start
        if self.point_end is not None:
            end = self.point_end
        points_map = {0.0: "min", 1.0: "max"}
        size = len(points) - 1
        return (
            start,
            end,
            {
                points_map.get(i / size, "%.2f%%" % (100 * i / size,)): j
                for i, j in enumerate(points)
            },
        )

    def GET_with_session(self, request, key):
        if not request.GET.get(key):
            return request.session.get(key, False)
        return request.GET.get(key, None) == "1"

    def setup_problem_list(self, request):
        self.hide_solved = self.GET_with_session(request, "hide_solved")
        self.show_types = self.GET_with_session(request, "show_types")
        self.full_text = self.GET_with_session(request, "full_text")
        self.show_editorial = self.GET_with_session(request, "show_editorial")
        self.show_solved_only = self.GET_with_session(request, "show_solved_only")

        self.search_query = None
        self.category = None
        self.org_query = []
        self.author_query = []
        self.selected_types = []

        # This actually copies into the instance dictionary...
        self.all_sorts = set(self.all_sorts)
        if not self.show_types:
            self.all_sorts.discard("type")

        self.category = safe_int_or_none(request.GET.get("category"))
        if "type" in request.GET:
            try:
                self.selected_types = list(map(int, request.GET.getlist("type")))
            except ValueError:
                pass
        if "orgs" in request.GET:
            try:
                self.org_query = list(map(int, request.GET.getlist("orgs")))
            except ValueError:
                pass
        if "authors" in request.GET:
            try:
                self.author_query = list(map(int, request.GET.getlist("authors")))
            except ValueError:
                pass

        self.point_start = safe_float_or_none(request.GET.get("point_start"))
        self.point_end = safe_float_or_none(request.GET.get("point_end"))

    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)

        try:
            return super(ProblemList, self).get(request, *args, **kwargs)
        except ProgrammingError as e:
            return generic_message(request, "FTS syntax error", e.args[1], status=400)

    def post(self, request, *args, **kwargs):
        to_update = (
            "hide_solved",
            "show_types",
            "full_text",
            "show_editorial",
            "show_solved_only",
        )
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == "1"
                request.session[key] = val
            else:
                request.session[key] = False
        return HttpResponseRedirect(request.get_full_path())


class ProblemFeed(ProblemList, FeedView):
    model = Problem
    context_object_name = "problems"
    template_name = "problem/feed.html"
    feed_content_template_name = "problem/feed/items.html"
    paginate_by = 4
    title = _("Problem feed")
    feed_type = None

    def get_recommended_problem_ids(self, problem_ids):
        user_id = self.request.profile.id

        # Check if two-tower model is available
        two_tower_available = False
        try:
            from ml.two_tower.serving import get_recommender

            two_tower_available = get_recommender() is not None
        except Exception:
            pass

        if two_tower_available:
            # Use two-tower model as primary, with collab filter as fallback
            rec_types = [
                RecommendationType.TWO_TOWER,
                RecommendationType.CF_DOT,
                RecommendationType.CF_COSINE,
                RecommendationType.HOT_PROBLEM,
            ]
            limits = [300, 50, 50, 20]
        else:
            # Fall back to original collab filter
            rec_types = [
                RecommendationType.CF_DOT,
                RecommendationType.CF_COSINE,
                RecommendationType.CF_TIME_DOT,
                RecommendationType.CF_TIME_COSINE,
                RecommendationType.HOT_PROBLEM,
            ]
            limits = [100, 100, 100, 100, 20]

        shuffle = True

        allow_debug_type = (
            self.request.user.is_impersonate or self.request.user.is_superuser
        )
        if allow_debug_type and "debug_type" in self.request.GET:
            try:
                debug_type = int(self.request.GET.get("debug_type"))
            except ValueError:
                raise Http404()
            rec_types = [debug_type]
            limits = [100]
            shuffle = False

        return get_user_recommended_problems(
            user_id, problem_ids, rec_types, limits, shuffle
        )

    def get_queryset(self):
        queryset = super(ProblemFeed, self).get_queryset()

        if self.feed_type == "new":
            return queryset.order_by("-date").values_list("id", flat=True)

        if "search" in self.request.GET:
            return queryset.values_list("id", flat=True)

        if not settings.ML_OUTPUT_PATH or not self.request.profile:
            return queryset.order_by("?").values_list("id", flat=True)

        return self.get_recommended_problem_ids(queryset.values_list("id", flat=True))

    def get_feed_context(self, object_list):
        Problem.prefetch_cache_description(self.request.LANGUAGE_CODE, *object_list)

        return {
            "completed_problem_ids": self.get_completed_problems(),
            "attempted_problems": self.get_attempted_problems(),
            "show_types": self.show_types,
            "problems": Problem.get_cached_instances(*object_list),
        }

    def get_context_data(self, **kwargs):
        context = super(ProblemFeed, self).get_context_data(**kwargs)
        context["page_type"] = "feed"
        context["title"] = self.title
        context["feed_type"] = self.feed_type
        context["has_show_editorial_option"] = False

        problem_ids = [problem.id for problem in context["problems"]]
        Problem.prefetch_cache_description(self.request.LANGUAGE_CODE, *problem_ids)

        return context

    def get(self, request, *args, **kwargs):
        if request.in_contest_mode:
            return HttpResponseRedirect(reverse("problem_list"))
        return super(ProblemFeed, self).get(request, *args, **kwargs)


class LanguageTemplateAjax(View):
    def get(self, request, *args, **kwargs):
        try:
            problem = request.GET.get("problem", None)
            lang_id = int(request.GET.get("id", 0))
            res = None
            if problem:
                try:
                    res = LanguageTemplate.objects.get(
                        language__id=lang_id, problem__id=problem
                    ).source
                except ObjectDoesNotExist:
                    pass
            if not res:
                res = get_object_or_404(Language, id=lang_id).template
        except ValueError:
            raise Http404()
        return HttpResponse(res, content_type="text/plain")


class RandomProblem(ProblemList):
    def get(self, request, *args, **kwargs):
        self.setup_problem_list(request)

        if self.in_contest:
            raise Http404()

        try:
            problem_ids = self.get_normal_queryset()
            count = problem_ids.count()
        except ProgrammingError as e:
            return generic_message(request, "FTS syntax error", e.args[1], status=400)

        if not count:
            query_string = request.META.get("QUERY_STRING", "")
            return HttpResponseRedirect(
                "%s%s%s"
                % (
                    reverse("problem_list"),
                    query_string and "?",
                    query_string,
                )
            )

        # Get a random problem ID and fetch the Problem object
        random_problem_id = problem_ids[randrange(count)]
        try:
            problem = Problem.objects.get(id=random_problem_id)
            return HttpResponseRedirect(problem.get_absolute_url())
        except Problem.DoesNotExist:
            # Fallback to problem list if the problem was deleted between queries
            query_string = request.META.get("QUERY_STRING", "")
            return HttpResponseRedirect(
                "%s%s%s"
                % (
                    reverse("problem_list"),
                    query_string and "?",
                    query_string,
                )
            )


user_logger = logging.getLogger("judge.user")


def last_nth_submitted_date_in_contest(profile, contest, n):
    submissions = Submission.objects.filter(
        user=profile, contest_object=contest
    ).order_by("-id")[:n]
    if submissions.count() >= n:
        return submissions[n - 1].date
    return None


@login_required
def problem_submit(request, problem, submission=None):
    if (
        submission is not None
        and not request.user.has_perm("judge.resubmit_other")
        and get_object_or_404(Submission, id=int(submission)).user.user != request.user
    ):
        raise PermissionDenied()

    profile = request.profile
    problem = get_object_or_404(Problem, code=problem)
    if not problem.is_accessible_by(request.user):
        if request.method == "POST":
            user_logger.info(
                "Naughty user %s wants to submit to %s without permission",
                request.user.username,
                problem.code,
            )
            return HttpResponseForbidden("<h1>Not allowed to submit. Try later.</h1>")
        raise Http404()

    if problem.is_editable_by(request.user):
        judge_choices = tuple(
            Judge.objects.filter(online=True, problems=problem).values_list(
                "name", "name"
            )
        )
    else:
        judge_choices = ()

    if request.method == "POST":
        form = ProblemSubmitForm(
            request.POST,
            request.FILES,
            judge_choices=judge_choices,
            instance=Submission(user=profile, problem=problem),
            request=request,
            problem=problem,
        )
        if form.is_valid():
            if (
                not request.user.has_perm("judge.spam_submission")
                and Submission.objects.filter(user=profile, was_rejudged=False)
                .exclude(status__in=["D", "IE", "CE", "AB"])
                .count()
                >= settings.DMOJ_SUBMISSION_LIMIT
            ):
                return HttpResponse(
                    _("<h1>You have submitted too many submissions.</h1>"), status=429
                )
            if not problem.allowed_languages.filter(
                id=form.cleaned_data["language"].id
            ).exists():
                raise PermissionDenied()
            if (
                not request.user.is_superuser
                and problem.banned_users.filter(id=profile.id).exists()
            ):
                return generic_message(
                    request,
                    _("Banned from submitting"),
                    _(
                        "You have been declared persona non grata for this problem. "
                        "You are permanently barred from submitting this problem."
                    ),
                )

            with transaction.atomic():
                if profile.current_contest is not None:
                    contest = profile.current_contest.contest
                    contest_id = profile.current_contest.contest_id
                    rate_limit = contest.rate_limit

                    if rate_limit:
                        t = last_nth_submitted_date_in_contest(
                            profile, contest, rate_limit
                        )
                        if t is not None and timezone.now() - t < timezone.timedelta(
                            minutes=1
                        ):
                            return HttpResponse(
                                _("<h1>You have submitted too many submissions.</h1>"),
                                status=429,
                            )

                    try:
                        contest_problem = problem.contests.get(contest_id=contest_id)
                    except ContestProblem.DoesNotExist:
                        model = form.save()
                    else:
                        max_subs = contest_problem.max_submissions
                        if (
                            max_subs
                            and get_contest_submission_count(
                                problem, profile, profile.current_contest.virtual
                            )
                            >= max_subs
                        ):
                            return generic_message(
                                request,
                                _("Too many submissions"),
                                _(
                                    "You have exceeded the submission limit for this problem."
                                ),
                            )
                        model = form.save()
                        model.contest_object_id = contest_id

                        contest = ContestSubmission(
                            submission=model,
                            problem=contest_problem,
                            participation=profile.current_contest,
                        )
                        contest.save()
                else:
                    model = form.save()

                # Create the SubmissionSource object
                source = SubmissionSource(
                    submission=model, source=form.cleaned_data["source"]
                )
                source.save()
                profile.update_contest()

            # Save a query
            model.source = source
            model.judge(rejudge=False, judge_id=form.cleaned_data["judge"])

            return HttpResponseRedirect(
                reverse("submission_status", args=[str(model.id)])
            )
        else:
            form_data = form.cleaned_data
            if submission is not None:
                sub = get_object_or_404(Submission, id=int(submission))
    else:
        initial = {"language": profile.language}
        if submission is not None:
            try:
                sub = get_object_or_404(
                    Submission.objects.select_related("source", "language"),
                    id=int(submission),
                )
                initial["source"] = sub.source.source
                initial["language"] = sub.language
            except ValueError:
                raise Http404()
        form = ProblemSubmitForm(judge_choices=judge_choices, initial=initial)
        form_data = initial
    form.fields["language"].queryset = problem.usable_languages.order_by(
        "name", "key"
    ).prefetch_related(
        Prefetch("runtimeversion_set", RuntimeVersion.objects.order_by("priority"))
    )
    if "language" in form_data:
        form.fields["source"].widget.mode = form_data["language"].ace
    form.fields["source"].widget.theme = profile.ace_theme

    if submission is not None:
        default_lang = sub.language
    else:
        default_lang = request.profile.language

    submission_limit = submissions_left = None
    next_valid_submit_time = None
    if profile.current_contest is not None:
        contest = profile.current_contest.contest
        try:
            submission_limit = problem.contests.get(contest=contest).max_submissions
        except ContestProblem.DoesNotExist:
            pass
        else:
            if submission_limit:
                submissions_left = submission_limit - get_contest_submission_count(
                    problem, profile, profile.current_contest.virtual
                )
        if contest.rate_limit:
            t = last_nth_submitted_date_in_contest(profile, contest, contest.rate_limit)
            if t is not None:
                next_valid_submit_time = t + timezone.timedelta(minutes=1)
                next_valid_submit_time = next_valid_submit_time.isoformat()

    return render(
        request,
        "problem/submit.html",
        {
            "form": form,
            "title": _("Submit to %(problem)s")
            % {
                "problem": problem.translated_name(request.LANGUAGE_CODE),
            },
            "content_title": mark_safe(
                escape(_("Submit to %(problem)s"))
                % {
                    "problem": format_html(
                        '<a href="{0}">{1}</a>',
                        reverse("problem_detail", args=[problem.code]),
                        problem.translated_name(request.LANGUAGE_CODE),
                    ),
                }
            ),
            "langs": Language.objects.all(),
            "no_judges": not form.fields["language"].queryset,
            "submission_limit": submission_limit,
            "submissions_left": submissions_left,
            "ACE_URL": settings.ACE_URL,
            "default_lang": default_lang,
            "problem_id": problem.id,
            "output_only": (
                problem.data_files.output_only
                if hasattr(problem, "data_files")
                else False
            ),
            "next_valid_submit_time": next_valid_submit_time,
        },
    )


class ProblemClone(
    ProblemMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView
):
    title = _("Clone Problem")
    template_name = "problem/clone.html"
    form_class = ProblemCloneForm
    permission_required = "judge.clone_problem"

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False

        # Ensure the user has clone_problem permission
        if not self.request.user.has_perm("judge.clone_problem"):
            return False

        # Additional checks to ensure problem is accessible
        problem = self.get_object()
        return problem.is_accessible_by(self.request.user)

    def form_valid(self, form):
        languages = self.object.allowed_languages.all()
        language_limits = self.object.language_limits.all()
        types = self.object.types.all()

        problem = deepcopy(self.object)

        problem.pk = None
        problem.is_public = False
        problem.ac_rate = 0
        problem.user_count = 0
        problem.code = form.cleaned_data["code"]
        problem._bypass_points_cap = self.request.user.is_superuser
        problem.save(should_move_data=False)
        problem.authors.add(self.request.profile)
        problem.allowed_languages.set(languages)
        problem.language_limits.set(language_limits)
        problem.types.set(types)

        return HttpResponseRedirect(
            reverse("admin:judge_problem_change", args=(problem.id,))
        )


class ProblemEdit(
    ProblemMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView
):
    title = _("Edit Problem")
    template_name = "problem/edit.html"
    form_class = ProblemEditForm

    def get_title(self):
        return _("Edit {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Editing problem for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False

        problem = self.get_object()
        return problem.is_editable_by(self.request.user)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # Add the instance to form kwargs (since SingleObjectFormView doesn't include ModelFormMixin)
        if hasattr(self, "object"):
            kwargs.update({"instance": self.object})

        # Pass the user to the form for validation
        kwargs["user"] = self.request.user

        # Set initial memory unit based on current memory_limit
        if self.object and self.object.memory_limit:
            # If memory is divisible by 1024, show in MB
            if self.object.memory_limit % 1024 == 0:
                kwargs.setdefault("initial", {})
                kwargs["initial"]["memory_unit"] = "MB"
                kwargs["initial"]["memory_limit"] = self.object.memory_limit // 1024
            else:
                kwargs.setdefault("initial", {})
                kwargs["initial"]["memory_unit"] = "KB"
        return kwargs

    def form_valid(self, form):
        # Save the main form
        problem = form.save(commit=False)

        # Handle memory unit conversion (already done in clean method)
        (
            form.changed_data.remove("memory_unit")
            if "memory_unit" in form.changed_data
            else None
        )

        problem._bypass_points_cap = self.request.user.is_superuser
        problem.save()
        form.save_m2m()

        # Add the current user as a curator if they're not already an author/curator
        if not problem.is_editor(self.request.profile):
            problem.curators.add(self.request.profile)

        # Rescore if necessary fields changed
        if form.changed_data and any(
            f in form.changed_data for f in ("is_public", "points", "partial")
        ):
            transaction.on_commit(rescore_problem.s(problem.id).delay)

        # Redirect to refresh the page with updated data (POST-REDIRECT-GET pattern)
        return HttpResponseRedirect(self.request.path)

    def form_invalid(self, form):
        """Handle invalid form with formsets"""
        return self.render_to_response(self.get_context_data(form=form))

    def post(self, request, *args, **kwargs):
        """Handle POST requests - either AI tagging, markdown improvement, or regular form submission"""
        self.object = self.get_object()

        # Check if this is an AI tagging request
        if request.POST.get("problem_tag") == "1":
            return self.handle_problem_tag(request)

        # Check if this is a markdown improvement request
        if request.POST.get("improve_markdown") == "1":
            return self.handle_improve_markdown(request)

        # Otherwise, handle regular form submission
        return super().post(request, *args, **kwargs)

    def handle_problem_tag(self, request):
        """Handle AI tagging request - dispatches async Celery task"""
        try:
            # Check if user is superuser (only superusers can use AI tagging)
            if not request.user.is_superuser:
                return JsonResponse({"success": False, "error": "Permission denied"})

            problem_code = request.POST.get("problem_code")
            if not problem_code:
                return JsonResponse(
                    {"success": False, "error": "Problem code is required"}
                )

            try:
                problem = Problem.objects.get(code=problem_code)
            except Problem.DoesNotExist:
                return JsonResponse({"success": False, "error": "Problem not found"})

            # Dispatch async Celery task
            task = tag_problem_task.delay(problem_code)

            return JsonResponse(
                {
                    "success": True,
                    "task_id": task.id,
                    "status": "processing",
                    "problem_code": problem.code,
                }
            )

        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Error in AI tagging: {e}")
            return JsonResponse(
                {"success": False, "error": "An unexpected error occurred"}
            )

    def handle_improve_markdown(self, request):
        """Handle markdown improvement request - dispatches async Celery task"""
        try:
            # Check if user is superuser (only superusers can use this feature)
            if not request.user.is_superuser:
                return JsonResponse({"success": False, "error": "Permission denied"})

            problem_code = request.POST.get("problem_code")
            if not problem_code:
                return JsonResponse(
                    {"success": False, "error": "Problem code is required"}
                )

            try:
                problem = Problem.objects.get(code=problem_code)
            except Problem.DoesNotExist:
                return JsonResponse({"success": False, "error": "Problem not found"})

            # Dispatch async Celery task
            task = improve_markdown_task.delay(problem_code)

            return JsonResponse(
                {
                    "success": True,
                    "task_id": task.id,
                    "status": "processing",
                    "problem_code": problem.code,
                }
            )

        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.error(f"Error in markdown improvement: {e}")
            return JsonResponse(
                {"success": False, "error": "An unexpected error occurred"}
            )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["problem"] = self.object
        return context


class ProblemAdd(PermissionRequiredMixin, CreateView):
    title = _("Add Problem")
    template_name = "problem/add.html"
    form_class = ProblemAddForm
    model = Problem

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False

        # Check if user is superuser or has explicit add problem permission
        user = self.request.user
        return user.is_superuser or user.has_perm("judge.add_problem")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        """Handle valid form submission"""
        problem = form.save()

        # Set default values for new problem
        if not problem.date:
            problem.date = timezone.now()

        problem._bypass_points_cap = self.request.user.is_superuser
        problem.save()

        # Add success message
        messages.success(
            self.request,
            _("Problem '%(name)s' has been created successfully!")
            % {"name": problem.name},
        )

        return HttpResponseRedirect(reverse("problem_detail", args=[problem.code]))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = self.title
        context["page_type"] = "add"
        return context


class ProblemEditLanguageLimits(
    ProblemMixin,
    PermissionRequiredMixin,
    TitleMixin,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    """Manage language-specific limits for a problem"""

    template_name = "problem/language_limits.html"

    def get_title(self):
        return _("Edit Language Limits - {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Editing language limits for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False

        problem = self.get_object()
        return problem.is_editable_by(self.request.user)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        language_limits = self.object.language_limits.all().order_by("language__name")

        # Create form for adding new language limit
        form = LanguageLimitEditForm(problem=self.object)

        return self.render_to_response(
            {
                "language_limits": language_limits,
                "problem": self.object,
                "form": form,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
            }
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        # Handle delete requests
        if "delete_limit" in request.POST:
            limit_id = request.POST.get("limit_id")
            try:
                limit = self.object.language_limits.get(id=limit_id)
                limit.delete()
            except LanguageLimit.DoesNotExist:
                pass
            return HttpResponseRedirect(
                reverse("problem_edit_language_limits", args=[self.object.code])
            )

        # Handle add form submission
        form = LanguageLimitEditForm(request.POST, problem=self.object)

        if form.is_valid():
            language_limit = form.save(commit=False)
            language_limit.problem = self.object
            language_limit.save()
            return HttpResponseRedirect(
                reverse("problem_edit_language_limits", args=[self.object.code])
            )

        # Form is invalid, redisplay with errors
        language_limits = self.object.language_limits.all().order_by("language__name")
        return self.render_to_response(
            {
                "language_limits": language_limits,
                "problem": self.object,
                "form": form,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
            }
        )


class ProblemEditLanguageTemplates(
    ProblemMixin,
    PermissionRequiredMixin,
    TitleMixin,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    """Manage language templates for a problem"""

    template_name = "problem/language_templates.html"

    def get_title(self):
        return _("Edit Language Templates - {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Editing language templates for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False

        problem = self.get_object()
        return problem.is_editable_by(self.request.user)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        language_templates = self.object.language_templates.all().order_by(
            "language__name"
        )

        # Create form for adding new language template
        form = LanguageTemplateEditForm(problem=self.object)

        return self.render_to_response(
            {
                "language_templates": language_templates,
                "problem": self.object,
                "form": form,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
                "ACE_URL": settings.ACE_URL,
            }
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        # Handle delete requests
        if "delete_template" in request.POST:
            template_id = request.POST.get("template_id")
            try:
                template = self.object.language_templates.get(id=template_id)
                template.delete()
                messages.success(request, _("Language template deleted successfully."))
            except LanguageTemplate.DoesNotExist:
                messages.error(request, _("Language template not found."))
            return HttpResponseRedirect(
                reverse("problem_edit_language_templates", args=[self.object.code])
            )

        # Handle edit requests
        if "edit_template" in request.POST:
            template_id = request.POST.get("template_id")
            try:
                template = self.object.language_templates.get(id=template_id)
                # Update the template directly without using form validation for simplicity
                new_source = request.POST.get("source", "")
                template.source = new_source
                template.save()
                messages.success(request, _("Language template updated successfully."))
                return HttpResponseRedirect(
                    reverse("problem_edit_language_templates", args=[self.object.code])
                )
            except LanguageTemplate.DoesNotExist:
                messages.error(request, _("Language template not found."))
                return HttpResponseRedirect(
                    reverse("problem_edit_language_templates", args=[self.object.code])
                )

        # Handle add form submission
        form = LanguageTemplateEditForm(request.POST, problem=self.object)

        if form.is_valid():
            language_template = form.save(commit=False)
            language_template.problem = self.object
            language_template.save()
            messages.success(request, _("Language template added successfully."))
            return HttpResponseRedirect(
                reverse("problem_edit_language_templates", args=[self.object.code])
            )

        # Form is invalid, redisplay with errors
        language_templates = self.object.language_templates.all().order_by(
            "language__name"
        )
        return self.render_to_response(
            {
                "language_templates": language_templates,
                "problem": self.object,
                "form": form,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
                "ACE_URL": settings.ACE_URL,
            }
        )


class ProblemEditSolutions(
    ProblemMixin,
    PermissionRequiredMixin,
    TitleMixin,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    """Manage solutions for a problem"""

    template_name = "problem/solutions.html"

    def get_title(self):
        return _("Edit Solutions - {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Editing solutions for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False

        problem = self.get_object()
        return problem.is_editable_by(self.request.user)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        solutions = Solution.objects.filter(problem=self.object)
        existing_solution = solutions.first() if solutions.exists() else None

        # If there's an existing solution, create an edit form
        if existing_solution:
            form = ProblemSolutionEditForm(instance=existing_solution)
        else:
            form = ProblemSolutionEditForm()

        return self.render_to_response(
            {
                "solutions": solutions,
                "problem": self.object,
                "form": form,
                "existing_solution": existing_solution,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
            }
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        # Handle AJAX request for auto-generating solution
        if "generate_solution" in request.POST:
            return self.handle_generate_solution(request)

        solutions = Solution.objects.filter(problem=self.object)
        existing_solution = solutions.first() if solutions.exists() else None

        # Handle delete requests
        if "delete_solution" in request.POST:
            solution_id = request.POST.get("solution_id")
            try:
                solution = Solution.objects.get(id=solution_id, problem=self.object)
                solution.delete()

                messages.success(request, _("Solution deleted successfully."))
            except Solution.DoesNotExist:
                messages.error(request, _("Solution not found."))
            return HttpResponseRedirect(
                reverse("problem_edit_solutions", args=[self.object.code])
            )

        # Handle add/edit form submission
        if existing_solution:
            # Edit existing solution
            form = ProblemSolutionEditForm(request.POST, instance=existing_solution)
            success_message = _("Solution updated successfully.")
        else:
            # Check if a solution was created while we were editing
            if Solution.objects.filter(problem=self.object).exists():
                messages.error(
                    request, _("A solution already exists for this problem.")
                )
                return HttpResponseRedirect(
                    reverse("problem_edit_solutions", args=[self.object.code])
                )

            # Add new solution
            form = ProblemSolutionEditForm(request.POST)
            success_message = _("Solution added successfully.")

        if form.is_valid():
            solution = form.save(commit=False)
            if not existing_solution:
                solution.problem = self.object
            solution.save()
            form.save_m2m()  # Save many-to-many relationships

            messages.success(request, success_message)
            return HttpResponseRedirect(
                reverse("problem_edit_solutions", args=[self.object.code])
            )

        # If form is invalid, show the page with errors
        solutions = Solution.objects.filter(problem=self.object)
        existing_solution = solutions.first() if solutions.exists() else None
        return self.render_to_response(
            {
                "solutions": solutions,
                "problem": self.object,
                "form": form,
                "existing_solution": existing_solution,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
            }
        )

    def handle_generate_solution(self, request):
        """Handle AJAX request for auto-generating solution using LLM - dispatches async Celery task"""
        if not request.user.is_superuser:
            return JsonResponse({"success": False, "error": "Permission denied"})

        try:
            problem = self.object
            rough_ideas = request.POST.get("rough_ideas", "").strip()

            # Dispatch async Celery task
            task = generate_solution_task.delay(problem.code, rough_ideas)

            return JsonResponse(
                {
                    "success": True,
                    "task_id": task.id,
                    "status": "processing",
                    "problem_code": problem.code,
                }
            )

        except Exception as e:
            return JsonResponse({"success": False, "error": str(e)})


class ProblemEditTranslations(
    ProblemMixin,
    PermissionRequiredMixin,
    TitleMixin,
    TemplateResponseMixin,
    SingleObjectMixin,
    View,
):
    """Manage translations for a problem"""

    template_name = "problem/translations.html"

    def get_title(self):
        return _("Edit Translations - {0}").format(self.object.name)

    def get_content_title(self):
        return mark_safe(
            escape(_("Editing translations for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.object.name,
                    reverse("problem_detail", args=[self.object.code]),
                )
            )
        )

    def has_permission(self):
        if not self.request.user.is_authenticated:
            return False

        problem = self.get_object()
        return problem.is_editable_by(self.request.user)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        translations = self.object.translations.all()
        form = ProblemTranslationEditForm(prefix="add")

        # Create individual forms for each existing translation
        translation_forms = []
        for translation in translations:
            translation_form = ProblemTranslationEditForm(
                instance=translation, prefix=f"edit_{translation.id}"
            )
            translation_forms.append(
                {"translation": translation, "form": translation_form}
            )

        return self.render_to_response(
            {
                "translations": translations,
                "translation_forms": translation_forms,
                "problem": self.object,
                "form": form,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
            }
        )

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        # Handle delete requests
        if "delete_translation" in request.POST:
            translation_id = request.POST.get("translation_id")
            try:
                translation = self.object.translations.get(id=translation_id)
                translation.delete()
                messages.success(request, _("Translation deleted successfully."))
            except ProblemTranslation.DoesNotExist:
                messages.error(request, _("Translation not found."))
            return HttpResponseRedirect(
                reverse("problem_edit_translations", args=[self.object.code])
            )

        # Handle edit requests
        if "edit_translation" in request.POST:
            translation_id = request.POST.get("translation_id")
            try:
                translation = self.object.translations.get(id=translation_id)
                prefix = f"edit_{translation.id}"

                # Use Django's built-in prefix handling - pass the full POST data with prefix
                form = ProblemTranslationEditForm(
                    request.POST, instance=translation, prefix=prefix
                )

                if form.is_valid():
                    saved_translation = form.save()
                    messages.success(request, _("Translation updated successfully."))
                    return HttpResponseRedirect(
                        reverse("problem_edit_translations", args=[self.object.code])
                    )
                else:
                    # If form has errors, render the page with errors
                    translations = self.object.translations.all()
                    translation_forms = []
                    for trans in translations:
                        if trans.id == translation.id:
                            # Use the submitted form with errors for this translation
                            translation_forms.append(
                                {"translation": trans, "form": form}
                            )
                        else:
                            # Create clean forms for other translations
                            translation_forms.append(
                                {
                                    "translation": trans,
                                    "form": ProblemTranslationEditForm(
                                        instance=trans, prefix=f"edit_{trans.id}"
                                    ),
                                }
                            )

                    return self.render_to_response(
                        {
                            "translations": translations,
                            "translation_forms": translation_forms,
                            "problem": self.object,
                            "form": ProblemTranslationEditForm(prefix="add"),
                            "title": self.get_title(),
                            "content_title": self.get_content_title(),
                        }
                    )
            except ProblemTranslation.DoesNotExist:
                messages.error(request, _("Translation not found."))
                return HttpResponseRedirect(
                    reverse("problem_edit_translations", args=[self.object.code])
                )

        # Handle add form submission
        form = ProblemTranslationEditForm(request.POST, prefix="add")
        if form.is_valid():
            # Check for duplicate language
            language = form.cleaned_data["language"]
            if self.object.translations.filter(language=language).exists():
                messages.error(
                    request, _("A translation for this language already exists.")
                )
                return HttpResponseRedirect(
                    reverse("problem_edit_translations", args=[self.object.code])
                )

            translation = form.save(commit=False)
            translation.problem = self.object
            translation.save()
            messages.success(request, _("Translation added successfully."))
            return HttpResponseRedirect(
                reverse("problem_edit_translations", args=[self.object.code])
            )

        # If form is invalid, show the page with errors
        translations = self.object.translations.all()

        # Create individual forms for each existing translation
        translation_forms = []
        for translation in translations:
            translation_form = ProblemTranslationEditForm(
                instance=translation, prefix=f"edit_{translation.id}"
            )
            translation_forms.append(
                {"translation": translation, "form": translation_form}
            )

        # Keep the form with errors (it already has the prefix)

        return self.render_to_response(
            {
                "translations": translations,
                "translation_forms": translation_forms,
                "problem": self.object,
                "form": form,
                "title": self.get_title(),
                "content_title": self.get_content_title(),
            }
        )
