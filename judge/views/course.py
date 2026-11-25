import csv
import json
import math
import re
from types import SimpleNamespace
from collections import Counter
from copy import deepcopy
from functools import partial
from urllib.parse import urlencode
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import (
    Case,
    When,
    Value,
    IntegerField,
    Count,
    Max,
    Q,
    OuterRef,
    Subquery,
)
from django.forms import (
    inlineformset_factory,
    ModelForm,
    modelformset_factory,
)
from django.forms.models import BaseInlineFormSet
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpResponse, Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import floatformat
from django.urls import reverse_lazy, reverse
from django.utils.encoding import smart_str
from django.utils.html import escape, mark_safe, format_html
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, DetailView, View, CreateView
from django.views.generic.edit import FormView
from reversion import revisions

from judge.forms import (
    LessonCloneForm,
    AddCourseForm,
)
from judge.models import (
    Course,
    Contest,
    CourseLesson,
    CourseLessonPrerequisite,
    Profile,
    CourseLessonProblem,
    CourseContest,
    ContestProblem,
    CourseRole,
    Organization,
    CourseLessonQuiz,
    QuizAttempt,
    BestQuizAttempt,
    Problem,
    Submission,
)
from judge.models.course import (
    RoleInCourse,
    EDITABLE_ROLES,
    get_course_role_profile_ids,
)
from judge.services.member_batches import resolve_member_usernames
from judge.utils.course_grades import (
    hidden_lesson_quiz_ids,
    bulk_max_case_points_per_problem,
    _bulk_quiz_total_points,
    _prepare_lesson_progress_data,
    _calculate_lesson_summary,
    _build_lesson_grade_details,
    bulk_calculate_lessons_progress,
    _prepare_contest_progress_data,
    bulk_calculate_contests_progress,
    calculate_total_progress,
)
from judge.utils.course_prerequisites import (
    get_lesson_lock_status,
    get_lesson_prerequisites_info,
    require_lesson_access,
    mark_course_for_recalculation,
    reorder_course_items,
)
from judge.utils.formsets import validate_max_active_forms
from judge.utils.hidden_results import (
    mark_hidden_result_submissions,
)
from judge.utils.identity import SemanticIdentityModelFormSet, save_semantic_formset
from judge.widgets import (
    HeavyPreviewPageDownWidget,
    HeavySelect2MultipleWidget,
    HeavySelect2Widget,
    DateTimePickerWidget,
)
from judge.widgets.direct_upload import DirectUploadImageWidget, DirectUploadFormMixin
from judge.forms import HTMLDisplayWidget
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.ranker import ranker
from judge.utils.views import (
    SingleObjectFormView,
    TitleMixin,
    DiggPaginatorMixin,
    paginate_query_context,
)

MAX_COURSE_LESSONS = 100
MAX_COURSE_LESSON_PROBLEMS = 100
MAX_COURSE_LESSON_QUIZZES = 100


class CoursePermissionMixin:
    """Mixin to handle course creation permissions and context"""

    def get_can_create_course(self):
        """Check if user can create courses"""
        if not self.request.user.is_authenticated:
            return False

        if self.request.user.is_superuser:
            return True

        # Check if user is admin of any organization
        admin_org_ids = self.request.profile.get_admin_organization_ids()
        return bool(admin_org_ids)

    def get_course_context_data(self, context):
        """Add course-related context data"""
        context["can_create_course"] = self.get_can_create_course()
        return context


def annotate_course_discovery(queryset, sort_order):
    student_count = (
        CourseRole.objects.filter(course_id=OuterRef("pk"), role=RoleInCourse.STUDENT)
        .values("course_id")
        .annotate(n=Count("pk"))
        .values("n")
    )
    lesson_count = (
        CourseLesson.objects.filter(course_id=OuterRef("pk"), is_visible=True)
        .values("course_id")
        .annotate(n=Count("pk"))
        .values("n")
    )
    order = {
        "name": ("name", "pk"),
        "students": ("-student_count", "name", "pk"),
        "newest": ("-pk",),
    }[sort_order]
    return (
        queryset.annotate(
            student_count=Subquery(student_count),
            visible_lesson_count=Subquery(lesson_count),
        )
        .order_by(*order)
        .prefetch_related("organizations")
        .distinct()
    )


class CourseList(CoursePermissionMixin, DiggPaginatorMixin, ListView):
    model = Course
    template_name = "course/list.html"
    paginate_by = 10
    context_object_name = "courses"

    def get(self, request, *args, **kwargs):
        default_tab = "my" if request.user.is_authenticated else "joinable"
        allowed = ["my", "joinable"]
        self.current_tab = request.GET.get("tab", default_tab)
        if self.current_tab not in allowed:
            self.current_tab = default_tab
        self.search_query = request.GET.get("search", "").strip()
        self.role_filter = request.GET.get("role_filter", "")
        self.sort_order = request.GET.get("sort", "newest")
        if self.sort_order not in ("newest", "name", "students"):
            self.sort_order = "newest"
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        profile = self.request.profile if self.request.user.is_authenticated else None
        if self.current_tab == "my":
            queryset = Course.get_user_courses(profile)
            if self.role_filter in ("teaching", "student"):
                roles = (
                    EDITABLE_ROLES
                    if self.role_filter == "teaching"
                    else [RoleInCourse.STUDENT]
                )
                queryset = queryset.filter(
                    courserole__user=profile, courserole__role__in=roles
                )
        else:
            queryset = Course.get_joinable_courses(profile)
        if hasattr(self, "organization"):
            queryset = queryset.filter(organizations=self.organization)
        if self.search_query:
            text = self.search_query
            teacher_courses = (
                CourseRole.objects.filter(
                    role=RoleInCourse.TEACHER,
                )
                .filter(
                    Q(user__user__username__icontains=text)
                    | Q(user__user__first_name__icontains=text)
                )
                .values("course_id")
            )
            queryset = queryset.filter(
                Q(name__icontains=text)
                | Q(slug__icontains=text)
                | Q(about__icontains=text)
                | Q(organizations__name__icontains=text)
                | Q(pk__in=teacher_courses)
            )
        # Correlated count subqueries keep membership filters out of the counts.
        return annotate_course_discovery(queryset, self.sort_order)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        courses = list(context["courses"])
        course_ids = [course.pk for course in courses]
        teacher_rows = list(
            CourseRole.objects.filter(
                course_id__in=course_ids, role=RoleInCourse.TEACHER
            ).values_list("course_id", "user_id")
        )
        profiles = {
            profile.pk: profile
            for profile in Profile.get_cached_instances(*{pk for _, pk in teacher_rows})
        }
        teachers = {}
        for course_id, profile_id in teacher_rows:
            teachers.setdefault(course_id, []).append(profiles[profile_id])
        viewer_roles = (
            dict(
                CourseRole.objects.filter(
                    course_id__in=course_ids, user=self.request.profile
                ).values_list("course_id", "role")
            )
            if self.request.profile
            else {}
        )
        for course in courses:
            course.card_teachers = teachers.get(course.pk, [])
            course.viewer_role_code = viewer_roles.get(course.pk)
            course.viewer_role = dict(RoleInCourse.choices).get(
                course.viewer_role_code, ""
            )
        context.update(
            courses=courses,
            title=_("Courses"),
            page_type=self.current_tab,
            current_tab=self.current_tab,
            search_query=self.search_query,
            role_filter=self.role_filter,
            sort_order=self.sort_order,
        )
        context.update(paginate_query_context(self.request))
        return self.get_course_context_data(context)


class CourseAdd(CoursePermissionMixin, LoginRequiredMixin, TitleMixin, CreateView):
    model = Course
    template_name = "course/create.html"
    form_class = AddCourseForm

    def get_title(self):
        return _("Add Course")

    def dispatch(self, request, *args, **kwargs):
        # Auth-required route: anonymous users go to login (project convention).
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        if request.user.is_superuser:
            return super().dispatch(request, *args, **kwargs)

        if not hasattr(request, "profile") or not request.profile:
            raise Http404()

        admin_org_ids = request.profile.get_admin_organization_ids()
        if not admin_org_ids:
            raise Http404()

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "add"

        admin_org_ids = self.request.profile.get_admin_organization_ids()
        if admin_org_ids:
            context["organizations"] = Organization.get_cached_instances(*admin_org_ids)
        else:
            context["organizations"] = []

        context = self.get_course_context_data(context)

        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request

        # Check for organization parameter in URL path (for organization-specific course creation)
        org_id = self.kwargs.get("org_id")
        if org_id:
            org = get_object_or_404(Organization, id=org_id)
            if (
                not org.is_admin(self.request.profile)
                and not self.request.user.is_superuser
            ):
                raise Http404()
            kwargs["organization"] = org
        else:
            # Check for organization parameter in query string (from organization course list)
            org_param = self.request.GET.get("organization")
            if org_param:
                try:
                    org = get_object_or_404(Organization, id=int(org_param))
                    if (
                        org.is_admin(self.request.profile)
                        or self.request.user.is_superuser
                    ):
                        kwargs["organization"] = org
                except (ValueError, TypeError):
                    # Invalid organization ID, ignore
                    pass

        return kwargs

    def form_valid(self, form):
        with revisions.create_revision():
            course = form.save()  # Form now handles organizations assignment

            # Add creator as teacher
            CourseRole.objects.create(
                course=course, user=self.request.profile, role=RoleInCourse.TEACHER
            )

            revisions.set_comment(_("Created course"))
            revisions.set_user(self.request.user)

            messages.success(self.request, _("Course created successfully."))
            return super().form_valid(form)

    def get_success_url(self):
        return reverse("course_detail", args=[self.object.slug])


class CourseDetailMixin(object):
    required_course_capability = None

    @transaction.atomic
    def dispatch(self, request, *args, **kwargs):
        if self.required_course_capability and not request.user.is_authenticated:
            if getattr(self, "json_endpoint", False):
                return JsonResponse(
                    {"error": str(_("Authentication required."))}, status=401
                )
            return redirect_to_login(request.get_full_path())
        queryset = (
            Course.objects.select_for_update()
            if request.method == "POST"
            else Course.objects.all()
        )
        self.course = get_object_or_404(queryset, slug=self.kwargs["slug"])
        self.course_role = None
        if request.user.is_authenticated:
            self.course_role = (
                CourseRole.objects.filter(course=self.course, user=request.profile)
                .values_list("role", flat=True)
                .first()
            )
        self.is_accessible = request.user.is_superuser or self.course_role is not None
        self.is_editable = (
            request.user.is_superuser or self.course_role in EDITABLE_ROLES
        )
        if self.required_course_capability == "edit" and not self.is_editable:
            raise Http404()
        if self.required_course_capability == "access" and not self.is_accessible:
            raise PermissionDenied(_("You are not enrolled in this course."))
        self.preview_profile = None
        self.viewer_profile = request.profile
        self.viewer_user = request.user
        preview = request.GET.get("preview", "")
        if preview and getattr(self, "allow_student_preview", False):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not self.is_editable or request.method != "GET":
                raise PermissionDenied()
            target_id = get_object_or_404(
                CourseRole,
                course=self.course,
                role=RoleInCourse.STUDENT,
                user__user__username=preview,
            ).user_id
            self.preview_profile = Profile.get_cached_instances(target_id)[0]
            self.viewer_profile = self.preview_profile
            self.viewer_user = User.objects.get(profile__pk=target_id)
            self.preview_profile.user = self.viewer_user
        return super().dispatch(request, *args, **kwargs)

    def get_user_role_in_course(self):
        return "ADMIN" if self.request.user.is_superuser else self.course_role

    def get_grade_students(self):
        if not self.course.show_classmates_grades and not self.is_editable:
            return (
                [self.request.profile]
                if self.course_role == RoleInCourse.STUDENT
                else []
            )
        return self.course.get_students()

    def check_grade_target(self, student):
        if (
            not self.course.show_classmates_grades
            and not self.is_editable
            and student.pk != self.request.profile.pk
        ):
            raise PermissionDenied(_("Only your own grades are available."))

    def render_to_response(self, context, **kwargs):
        if (
            getattr(self, "allow_grade_export", False)
            and self.request.GET.get("export") == "csv"
        ):
            response = HttpResponse(content_type="text/csv; charset=utf-8")
            response["Content-Disposition"] = (
                'attachment; filename="%s-grades-page.csv"' % self.course.slug
            )
            response.write("\ufeff")
            writer = csv.writer(response)
            writer.writerow([_("Username"), _("Name"), _("Percentage")])
            rows = (
                context.get("grade_total")
                if "grade_total" in context
                else {
                    profile: grade["total"]
                    for profile, grade in context.get("grades", {}).items()
                }
            )
            for profile, total in rows.items():
                name = profile.first_name or ""
                # Keep spreadsheet formulas as text when opening the CSV.
                safe = lambda value: (
                    "'" + value
                    if value.startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
                    else value
                )
                writer.writerow(
                    [
                        safe(profile.username),
                        safe(name),
                        (
                            _("Results hidden")
                            if total.get("is_result_hidden")
                            else round(total["percentage"], 2)
                        ),
                    ]
                )
            return response
        return super().render_to_response(context, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(CourseDetailMixin, self).get_context_data(**kwargs)
        context["course"] = self.course
        context["is_editable"] = self.is_editable and not self.preview_profile
        context["preview_profile"] = self.preview_profile
        context["preview_suffix"] = (
            "?" + urlencode({"preview": self.preview_profile.username})
            if self.preview_profile
            else ""
        )
        context["is_accessible"] = self.is_accessible
        context["own_grades_only"] = (
            not self.course.show_classmates_grades and not self.is_editable
        )
        return context


class CourseAccessibleMixin(CourseDetailMixin):
    """Requires the user to be enrolled in the course (any role)."""

    required_course_capability = "access"


class CourseEditableMixin(CourseDetailMixin):
    required_course_capability = "edit"


class CourseAdminMixin(CourseDetailMixin):
    required_course_capability = "edit"

    def user_can_manage_user(self, target_user_current_role):
        """Check if current user can manage users with the target_user_current_role"""
        current_role = self.get_user_role_in_course()

        # Admins can manage anyone
        if current_role == "ADMIN":
            return True

        # Teachers can manage assistants and students, but not other teachers
        if current_role == RoleInCourse.TEACHER:
            return target_user_current_role in [
                RoleInCourse.ASSISTANT,
                RoleInCourse.STUDENT,
            ]

        # Assistants can only manage students
        if current_role == RoleInCourse.ASSISTANT:
            return target_user_current_role == RoleInCourse.STUDENT

        # Students and non-members cannot manage anyone
        return False

    def user_can_assign_role(self, new_role):
        """Check if current user can assign the new_role to someone"""
        current_role = self.get_user_role_in_course()

        # Admins can assign any role
        if current_role == "ADMIN":
            return True

        # Teachers can assign any role (when they can manage the target user)
        if current_role == RoleInCourse.TEACHER:
            return True

        # Assistants can only assign assistant or student roles
        if current_role == RoleInCourse.ASSISTANT:
            return new_role in [RoleInCourse.ASSISTANT, RoleInCourse.STUDENT]

        # Students and non-members cannot assign roles
        return False


class CourseDetail(CourseDetailMixin, DetailView):
    allow_student_preview = True
    model = Course
    template_name = "course/course.html"

    def get_object(self):
        if not self.is_accessible:
            if not self.course.is_public:
                raise Http404()
            org_ids = set(self.course.organizations.values_list("pk", flat=True))
            viewer_orgs = (
                set(self.request.profile.get_organization_ids())
                if self.request.profile
                else set()
            )
            if org_ids and not org_ids.intersection(viewer_orgs):
                raise Http404()
        return self.course

    def get_template_names(self):
        if not self.is_accessible:
            return ["course/enrollment_required.html"]
        return [self.template_name]

    def get_context_data(self, **kwargs):
        context = super(CourseDetail, self).get_context_data(**kwargs)

        # If user doesn't have access, show enrollment message
        if not self.is_accessible:
            context["title"] = self.course.name
            context["page_type"] = "enrollment_required"

            # Check if user can join this course
            context["can_join"] = Course.is_joinable(self.course, self.request.profile)
            context["enrollment_reason"] = (
                _("Enrollment is closed. Contact the instructor for access.")
                if not self.course.is_open
                else _("Membership in a course organization is required to enroll.")
            )

            return context

        lessons = self.course.get_lessons()
        contests = self.course.get_contests()

        # Use bulk functions for single user (still more efficient due to better queries)
        students = [self.viewer_profile]
        progress_data = _prepare_lesson_progress_data(
            students, lessons, viewer=self.viewer_user
        )
        all_problem_ids = {
            row["problem_id"]
            for rows in progress_data["lesson_problems"].values()
            for row in rows
        }
        bulk_problem_points = bulk_max_case_points_per_problem(
            students, all_problem_ids, viewer=self.viewer_user
        )
        lesson_progress_bulk = bulk_calculate_lessons_progress(
            students, lessons, bulk_problem_points, progress_data=progress_data
        )
        contest_progress_bulk = bulk_calculate_contests_progress(
            students, contests, viewer=self.viewer_user
        )

        # Get user's role in the course
        user_role = None
        course_role = None
        if self.request.user.is_authenticated:
            try:
                course_role = CourseRole.objects.get(
                    course=self.course, user=self.request.profile
                )
                user_role = course_role.role
            except CourseRole.DoesNotExist:
                pass

        context["title"] = self.course.name
        context["page_type"] = "home"
        context["lessons"] = lessons
        context["lesson_progress"] = lesson_progress_bulk[self.viewer_profile]
        context["course_contests"] = contests
        context["contest_progress"] = contest_progress_bulk[self.viewer_profile]
        context["user_role"] = user_role

        context["total_progress"] = calculate_total_progress(
            context["lesson_progress"],
            context["contest_progress"],
        )

        # Add lesson lock status for prerequisites feature
        # Teachers/Assistants bypass lock
        if Course.is_editable_by(self.course, self.viewer_profile):
            context["lesson_lock_status"] = {lesson.id: False for lesson in lessons}
            context["lesson_prerequisites_info"] = {}
        else:
            context["lesson_lock_status"] = get_lesson_lock_status(
                self.viewer_profile, self.course
            )
            # Get prerequisites info for displaying what's needed
            prereq_info_by_order = get_lesson_prerequisites_info(self.course)

            # Convert to lesson_id based dict for template
            lesson_prerequisites_info = {}
            for lesson in lessons:
                if lesson.order in prereq_info_by_order:
                    lesson_prerequisites_info[lesson.id] = prereq_info_by_order[
                        lesson.order
                    ]
            context["lesson_prerequisites_info"] = lesson_prerequisites_info

        return context


class CourseLessonDetail(CourseAccessibleMixin, DetailView):
    allow_student_preview = True
    model = CourseLesson
    template_name = "course/lesson.html"

    def get_object(self):
        try:
            self.lesson = CourseLesson.objects.get(
                course=self.course, id=self.kwargs["id"]
            )

            require_lesson_access(self.lesson, self.viewer_user)

            return self.lesson
        except ObjectDoesNotExist:
            raise Http404()

    def is_lesson_unlocked_for_user(self, lesson, profile):
        """Check if lesson is unlocked for the user."""
        # Teachers/Assistants/Admins bypass lock
        if Course.is_editable_by(self.course, profile):
            return True

        lock_status = get_lesson_lock_status(profile, self.course)
        is_locked = lock_status.get(lesson.id, False)
        return not is_locked

    def get_profile(self):
        return self.viewer_profile

    def get_context_data(self, **kwargs):
        context = super(CourseLessonDetail, self).get_context_data(**kwargs)
        profile = self.get_profile()
        problems = self.lesson.get_problems()

        # Use bulk function even for single user for consistency
        bulk_problem_points = bulk_max_case_points_per_problem(
            [profile], problems, viewer=self.viewer_user
        )
        problem_points = bulk_problem_points.get(profile.id, {})

        context["profile"] = profile
        context["title"] = self.lesson.title
        context["lesson"] = self.lesson
        context["problem_points"] = problem_points
        context["completed_problem_ids"] = {
            problem_id
            for problem_id, points in problem_points.items()
            if points.get("case_total")
            and points.get("case_points", 0) >= points["case_total"]
        }
        context["attempted_problems"] = {
            problem_id
            for problem_id, points in problem_points.items()
            if points.get("case_total")
            and points.get("case_points", 0) < points["case_total"]
        }
        context["lesson_problems"] = [
            {"problem": p, "score": ps["score"]}
            for p, ps in zip(problems, self.lesson.get_problems_and_scores())
        ]

        # Get quizzes for this lesson
        lesson_quizzes = list(
            self.lesson.lesson_quizzes.filter(is_visible=True).select_related("quiz")
        )
        lesson_quiz_ids = [lesson_quiz.id for lesson_quiz in lesson_quizzes]
        quiz_totals = _bulk_quiz_total_points(
            {lesson_quiz.quiz_id for lesson_quiz in lesson_quizzes}
        )
        best_quiz_attempts = {
            row["lesson_quiz_id"]: row
            for row in BestQuizAttempt.objects.filter(
                user=profile, lesson_quiz_id__in=lesson_quiz_ids
            ).values("lesson_quiz_id", "score", "max_score")
        }
        attempt_counts = {
            row["lesson_quiz_id"]: row
            for row in QuizAttempt.objects.filter(
                user=profile, lesson_quiz_id__in=lesson_quiz_ids
            )
            .values("lesson_quiz_id")
            .annotate(
                attempts_count=Count("id"),
                submitted_attempts_count=Count("id", filter=Q(is_submitted=True)),
            )
        }

        hidden_quiz_ids = hidden_lesson_quiz_ids(
            [
                {
                    "id": link.pk,
                    "quiz_id": link.quiz_id,
                    "is_result_hidden": link.is_result_hidden,
                }
                for link in lesson_quizzes
            ],
            self.course,
            self.viewer_user,
        )
        # Get quiz data with user's lesson-linked best scores and attempt info.
        quiz_data = []
        for lesson_quiz in lesson_quizzes:
            quiz = lesson_quiz.quiz
            counts = attempt_counts.get(lesson_quiz.id, {})
            submitted_attempts_count = counts.get("submitted_attempts_count", 0)
            attempts_count = counts.get("attempts_count", 0)
            can_attempt = (
                lesson_quiz.max_attempts <= 0
                or attempts_count < lesson_quiz.max_attempts
            )

            quiz_data.append(
                {
                    "lesson_quiz": lesson_quiz,
                    "quiz": quiz,
                    "is_result_hidden": lesson_quiz.pk in hidden_quiz_ids,
                    "has_active_attempt": attempts_count > submitted_attempts_count,
                    "best_score": (
                        best_quiz_attempts[lesson_quiz.id]["score"]
                        if lesson_quiz.id in best_quiz_attempts
                        and lesson_quiz.pk not in hidden_quiz_ids
                        else None
                    ),
                    "max_score": (
                        best_quiz_attempts[lesson_quiz.id]["max_score"]
                        if lesson_quiz.id in best_quiz_attempts
                        else quiz_totals.get(quiz.id, 0)
                    ),
                    "attempts_count": submitted_attempts_count,
                    "max_attempts": lesson_quiz.max_attempts,
                    "can_attempt": can_attempt,
                    "points": lesson_quiz.points,
                }
            )

        context["lesson_quizzes"] = quiz_data

        context["sidebar_lessons"] = _get_unlocked_lessons(
            self.course,
            self.viewer_profile,
            self.is_editable and not self.preview_profile,
        )
        sidebar = list(context["sidebar_lessons"])
        index = next(
            (i for i, row in enumerate(sidebar) if row.pk == self.lesson.pk), None
        )
        context["previous_lesson"] = (
            sidebar[index - 1] if index is not None and index > 0 else None
        )
        context["next_lesson"] = (
            sidebar[index + 1]
            if index is not None and index + 1 < len(sidebar)
            else None
        )
        return context


class CourseLessonForm(forms.ModelForm):
    class Meta:
        model = CourseLesson
        fields = ["title", "is_visible", "points", "content", "order"]
        widgets = {
            "title": forms.TextInput(),
            "content": HeavyPreviewPageDownWidget(preview=reverse_lazy("blog_preview")),
            "problems": HeavySelect2MultipleWidget(data_view="problem_select2"),
            "order": forms.HiddenInput(),
        }


class CourseLessonModelFormSet(BaseInlineFormSet):
    def is_valid(self):
        valid = super().is_valid()
        if not valid:
            return valid
        return validate_max_active_forms(
            self,
            MAX_COURSE_LESSONS,
            _("A course may contain at most %(count)d lessons.")
            % {"count": MAX_COURSE_LESSONS},
        )


CourseLessonFormSet = inlineformset_factory(
    Course,
    CourseLesson,
    form=CourseLessonForm,
    formset=CourseLessonModelFormSet,
    extra=1,
    can_delete=True,
    max_num=MAX_COURSE_LESSONS,
)


class CourseLessonProblemForm(ModelForm):
    class Meta:
        model = CourseLessonProblem
        fields = ["order", "problem", "score", "lesson"]
        widgets = {
            "problem": HeavySelect2Widget(
                data_view="problem_select2", attrs={"style": "width: 100%"}
            ),
            "lesson": forms.HiddenInput(),
        }


class CourseLessonProblemModelFormSet(SemanticIdentityModelFormSet):
    semantic_identity_fields = ("problem",)

    def is_valid(self):
        valid = super().is_valid()
        if not valid:
            return valid
        return validate_max_active_forms(
            self,
            MAX_COURSE_LESSON_PROBLEMS,
            _("A lesson may contain at most %(count)d problems.")
            % {"count": MAX_COURSE_LESSON_PROBLEMS},
        )


CourseLessonProblemFormSet = modelformset_factory(
    CourseLessonProblem,
    form=CourseLessonProblemForm,
    formset=CourseLessonProblemModelFormSet,
    extra=0,
    can_delete=True,
    max_num=MAX_COURSE_LESSON_PROBLEMS,
)


class CourseLessonQuizForm(ModelForm):
    class Meta:
        model = CourseLessonQuiz
        fields = [
            "order",
            "quiz",
            "points",
            "max_attempts",
            "is_visible",
            "is_result_hidden",
            "lesson",
        ]
        widgets = {
            "quiz": HeavySelect2Widget(
                data_view="quiz_select2", attrs={"style": "width: 100%"}
            ),
            "lesson": forms.HiddenInput(),
        }


class CourseLessonQuizModelFormSet(SemanticIdentityModelFormSet):
    semantic_identity_fields = ("quiz",)

    def is_valid(self):
        valid = super().is_valid()
        if not valid:
            return valid
        return validate_max_active_forms(
            self,
            MAX_COURSE_LESSON_QUIZZES,
            _("A lesson may contain at most %(count)d quizzes.")
            % {"count": MAX_COURSE_LESSON_QUIZZES},
        )


CourseLessonQuizFormSet = modelformset_factory(
    CourseLessonQuiz,
    form=CourseLessonQuizForm,
    formset=CourseLessonQuizModelFormSet,
    extra=0,
    can_delete=True,
    max_num=MAX_COURSE_LESSON_QUIZZES,
)


class CreateCourseLesson(CourseEditableMixin, FormView):
    template_name = "course/create_lesson.html"
    form_class = CourseLessonFormSet
    other_form = CourseLessonForm
    model = CourseLesson

    def get_context_data(self, **kwargs):
        context = super(CreateCourseLesson, self).get_context_data(**kwargs)

        context["problem_formsets"] = CourseLessonProblemFormSet()
        context["title"] = _("Edit lessons for %(course_name)s") % {
            "course_name": self.course.name
        }
        context["content_title"] = mark_safe(
            _("Edit lessons for <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": escape(self.course.name),
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "edit_lesson_new"
        context["lesson_field"] = kwargs.get("form") or CourseLessonForm()

        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form(form_class=CourseLessonForm)  # Get the CourseLessonForm
        form.instance.course = self.course

        if form.is_valid():
            with revisions.create_revision():
                form.instance.course_id = self.course.id
                self.lesson = form.save()
                # Add revision tracking details
                revisions.set_comment(
                    _("Created lesson '{}' in course {}").format(
                        form.cleaned_data["title"], self.course.name
                    )
                )
                revisions.set_user(request.user)

            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def form_valid(self, form):
        return super().form_valid(form)

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def get_success_url(self):
        return reverse(
            "edit_course_lessons",
            args=[self.course.slug],
        )


class EditCourseLessonsViewNewWindow(CourseEditableMixin, FormView):
    template_name = "course/edit_lesson_new_window.html"
    form_class = CourseLessonFormSet
    other_form = CourseLessonForm
    model = CourseLesson

    def get_lesson(self):
        if not hasattr(self, "lesson"):
            self.lesson = get_object_or_404(
                CourseLesson, pk=self.kwargs["id"], course=self.course
            )
        return self.lesson

    def get(self, request, *args, **kwargs):
        self.get_lesson()
        return super().get(request, *args, **kwargs)

    def get_problem_formset(self, post=False, lesson=None):
        # Use the passed lesson parameter or fall back to self.lesson
        target_lesson = lesson if lesson is not None else self.lesson

        # Safety check
        if not target_lesson:
            raise ValueError("No lesson specified for problem formset")

        formset = CourseLessonProblemFormSet(
            data=self.request.POST if post else None,
            prefix=f"problems_{target_lesson.id}",
            queryset=CourseLessonProblem.objects.filter(lesson=target_lesson).order_by(
                "order"
            ),
        )
        formset.lesson = target_lesson
        for form in formset:
            form.fields["lesson"].initial = target_lesson
        # Also set the lesson for the empty_form (used as template for new rows)
        formset.empty_form.fields["lesson"].initial = target_lesson
        return formset

    def get_quiz_formset(self, post=False, lesson=None):
        # Use the passed lesson parameter or fall back to self.lesson
        target_lesson = lesson if lesson is not None else self.lesson

        # Safety check
        if not target_lesson:
            raise ValueError("No lesson specified for quiz formset")

        formset = CourseLessonQuizFormSet(
            data=self.request.POST if post else None,
            prefix=f"quizzes_{target_lesson.id}",
            queryset=CourseLessonQuiz.objects.filter(lesson=target_lesson).order_by(
                "order"
            ),
        )
        formset.lesson = target_lesson
        for form in formset:
            form.fields["lesson"].initial = target_lesson
        # Also set the lesson for the empty_form (used as template for new rows)
        formset.empty_form.fields["lesson"].initial = target_lesson
        return formset

    def get_context_data(self, **kwargs):
        self.get_lesson()
        context = super(EditCourseLessonsViewNewWindow, self).get_context_data(**kwargs)

        # Always provide formset for the template (needed for both GET and form_invalid)
        context["formset"] = self.form_class(
            instance=self.course, queryset=self.course.lessons.order_by("order")
        )
        # Create problem formset only for the current lesson
        context["problem_formsets"] = {
            self.lesson.id: kwargs.get("problem_formset")
            or self.get_problem_formset(post=False, lesson=self.lesson)
        }
        # Create quiz formset for the current lesson
        context["quiz_formset"] = kwargs.get("quiz_formset") or self.get_quiz_formset(
            post=False, lesson=self.lesson
        )

        context["title"] = _("Edit lessons for %(course_name)s") % {
            "course_name": self.course.name
        }
        context["content_title"] = mark_safe(
            _("Edit lessons for <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": escape(self.course.name),
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "edit_lesson_new"

        # Use form from kwargs if provided (for form_invalid), otherwise create new one
        if "form" in kwargs:
            context["lesson_field"] = kwargs["form"]
        else:
            context["lesson_field"] = CourseLessonForm(instance=self.lesson)

        context["lesson"] = self.lesson
        context["current_user_role"] = self.get_user_role_in_course()

        return context

    def post(self, request, *args, **kwargs):
        self.get_lesson()
        form = self.get_form(form_class=CourseLessonForm)  # Get the CourseLessonForm
        # Set course_id and pk BEFORE validation so clean() can check for duplicates
        form.instance.course_id = self.course.id
        form.instance.pk = self.lesson.id
        if form.is_valid():
            with revisions.create_revision():
                if "delete_lesson" in request.POST:
                    # Check if user has permission to delete lessons
                    current_role = self.get_user_role_in_course()
                    if current_role == RoleInCourse.ASSISTANT:
                        messages.error(request, _("Assistants cannot delete lessons."))
                        return HttpResponseRedirect(
                            reverse(
                                "edit_course_lessons_new",
                                args=[self.course.slug, self.lesson.id],
                            )
                        )

                    form.instance.course_id = self.course.id
                    form.instance.lesson_id = self.lesson.id

                    # Add revision tracking details for lesson deletion
                    revisions.set_comment(
                        _("Deleted lesson '{}' from course {}").format(
                            self.lesson.title, self.course.name
                        )
                    )
                    revisions.set_user(request.user)

                    self.lesson.delete()
                    messages.success(request, "Lesson deleted successfully.")
                    course_slug = self.course.slug
                    return HttpResponseRedirect(
                        reverse(
                            "edit_course_lessons",
                            args=[course_slug],
                        )
                    )
                else:
                    form.instance.course_id = self.course.id
                    form.instance.id = self.lesson.id

                    problem_formsets = self.get_problem_formset(
                        post=True, lesson=self.lesson
                    )
                    quiz_formsets = self.get_quiz_formset(post=True, lesson=self.lesson)
                    problem_formsets_valid = problem_formsets.is_valid()
                    quiz_formsets_valid = quiz_formsets.is_valid()
                    if not problem_formsets_valid or not quiz_formsets_valid:
                        return self.form_invalid(
                            form,
                            problem_formset=problem_formsets,
                            quiz_formset=quiz_formsets,
                        )

                    self.lesson = form.save()

                    # Add revision tracking details for lesson editing
                    revisions.set_comment(
                        _("Updated lesson '{}' in course {}").format(
                            form.cleaned_data["title"], self.course.name
                        )
                    )
                    revisions.set_user(request.user)

                    save_semantic_formset(
                        problem_formsets,
                        parent_field="lesson",
                        parent=self.lesson,
                        identity_fields=("problem",),
                    )

                    save_semantic_formset(
                        quiz_formsets,
                        parent_field="lesson",
                        parent=self.lesson,
                        identity_fields=("quiz",),
                    )
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def form_valid(self, form):
        if "delete_lesson" in self.request.POST:
            return redirect("edit_course_lessons", slug=self.course.slug)
        else:
            return super().form_valid(form)

    def form_invalid(self, form, *, problem_formset=None, quiz_formset=None):
        # Preserve bound values and validation errors after a failed save.
        return self.render_to_response(
            self.get_context_data(
                form=form,
                problem_formset=problem_formset,
                quiz_formset=quiz_formset,
            )
        )

    def get_success_url(self):
        return reverse(
            "edit_course_lessons",
            args=[self.course.slug],
        )


def valid_prerequisite_orders(course, source, target):
    orders = list(course.lessons.values_list("order", flat=True))
    if source == target or orders.count(source) != 1 or orders.count(target) != 1:
        return False
    return source < target


class EditCourseLessonsView(CourseEditableMixin, FormView):
    template_name = "course/edit_lesson.html"
    form_class = CourseLessonFormSet

    def get_problem_formset(self, post=False, lesson=None):
        formset = CourseLessonProblemFormSet(
            data=self.request.POST if post else None,
            prefix=f"problems_{lesson.id}" if lesson else "problems",
            queryset=CourseLessonProblem.objects.filter(lesson=lesson).order_by(
                "order"
            ),
        )
        formset.lesson = lesson
        if lesson:
            for form in formset:
                form.fields["lesson"].initial = lesson
        return formset

    def get_context_data(self, **kwargs):
        context = super(EditCourseLessonsView, self).get_context_data(**kwargs)

        # Get lessons with related problem data for the new design
        lessons = self.course.lessons.prefetch_related(
            "lesson_problems__problem"
        ).order_by("order")

        context["lessons"] = lessons
        context["title"] = _("Edit lessons for %(course_name)s") % {
            "course_name": self.course.name
        }
        context["content_title"] = mark_safe(
            _("Edit lessons for <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": escape(self.course.name),
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "edit_lesson"

        # Handle tab parameter for Lessons/Prerequisites/Order tabs
        tab = self.request.GET.get("tab", "lessons")
        context["tab"] = tab

        # Add prerequisites data for the prerequisites tab
        if tab == "prerequisites":

            prerequisites = CourseLessonPrerequisite.objects.filter(
                course=self.course
            ).order_by("source_order", "target_order")
            context["prerequisites"] = prerequisites

            # Build a dictionary mapping order -> lesson for easy lookup in template
            lessons_by_order = {lesson.order: lesson for lesson in lessons}
            context["lessons_by_order"] = lessons_by_order

        # Add contests data for the order tab
        if tab == "order":

            contests = (
                CourseContest.objects.filter(course=self.course)
                .order_by("order")
                .select_related("contest")
            )
            context["contests"] = contests

        return context

    def post(self, request, *args, **kwargs):

        action = request.POST.get("action")

        # Handle prerequisite AJAX actions
        if action == "add_prereq":
            source_order = request.POST.get("source_order")
            target_order = request.POST.get("target_order")
            required_percentage = request.POST.get("required_percentage", 0)

            try:
                source_order = int(source_order)
                target_order = int(target_order)
                required_percentage = float(required_percentage)

                if source_order >= target_order:
                    return JsonResponse(
                        {
                            "success": False,
                            "error": str(
                                _(
                                    "Source lesson order must be less than target lesson order."
                                )
                            ),
                        }
                    )

                if (
                    not math.isfinite(required_percentage)
                    or required_percentage < 0
                    or required_percentage > 100
                ):
                    return JsonResponse(
                        {
                            "success": False,
                            "error": str(_("Percentage must be between 0 and 100.")),
                        }
                    )

                if not valid_prerequisite_orders(
                    self.course, source_order, target_order
                ):
                    return JsonResponse(
                        {
                            "success": False,
                            "error": str(
                                _("Choose two distinct existing lesson orders.")
                            ),
                        },
                        status=400,
                    )
                prereq, created = CourseLessonPrerequisite.objects.get_or_create(
                    course=self.course,
                    source_order=source_order,
                    target_order=target_order,
                    defaults={"required_percentage": required_percentage},
                )

                if not created:
                    prereq.required_percentage = required_percentage
                    prereq.save()

                # Mark course for lazy recalculation for all enrolled users

                mark_course_for_recalculation(self.course)

                return JsonResponse(
                    {"success": True, "id": prereq.id, "created": created}
                )

            except (ValueError, TypeError):
                return JsonResponse(
                    {"success": False, "error": str(_("Invalid input."))}
                )

        elif action == "update_prereq_percentage":
            prereq_id = request.POST.get("prerequisite_id")
            try:
                prereq = CourseLessonPrerequisite.objects.get(
                    id=prereq_id, course=self.course
                )
                new_percentage = float(request.POST.get("required_percentage", 0))

                if (
                    not math.isfinite(new_percentage)
                    or new_percentage < 0
                    or new_percentage > 100
                ):
                    return JsonResponse(
                        {
                            "success": False,
                            "error": str(_("Percentage must be between 0 and 100.")),
                        }
                    )

                prereq.required_percentage = new_percentage
                prereq.save()

                # Mark course for lazy recalculation for all enrolled users

                mark_course_for_recalculation(self.course)

                return JsonResponse({"success": True})
            except CourseLessonPrerequisite.DoesNotExist:
                return JsonResponse(
                    {"success": False, "error": str(_("Prerequisite not found."))}
                )
            except (ValueError, TypeError):
                return JsonResponse(
                    {"success": False, "error": str(_("Invalid percentage value."))}
                )

        elif action == "remove_prereq":
            prereq_id = request.POST.get("prerequisite_id")
            try:
                prereq = CourseLessonPrerequisite.objects.get(
                    id=prereq_id, course=self.course
                )
                prereq.delete()

                # Mark course for lazy recalculation for all enrolled users

                mark_course_for_recalculation(self.course)

                return JsonResponse({"success": True})
            except CourseLessonPrerequisite.DoesNotExist:
                return JsonResponse(
                    {"success": False, "error": str(_("Prerequisite not found."))}
                )

        elif action == "bulk_update_prereqs":

            prerequisites_json = request.POST.get("prerequisites_json", "[]")
            try:
                prereqs_data = json.loads(prerequisites_json)
                if not isinstance(prereqs_data, list):
                    return JsonResponse(
                        {"success": False, "error": str(_("Invalid JSON format."))}
                    )

                # Validate all entries first
                lesson_rows = list(self.course.lessons.values_list("order", "pk"))
                counts = Counter(order for order, _ in lesson_rows)
                validated_prereqs = []
                for i, item in enumerate(prereqs_data):
                    if not isinstance(item, list) or len(item) != 3:
                        return JsonResponse(
                            {
                                "success": False,
                                "error": str(
                                    _("Invalid format in item %(num)s") % {"num": i + 1}
                                ),
                            }
                        )

                    if any(
                        isinstance(value, bool)
                        or (
                            isinstance(value, float)
                            and (not math.isfinite(value) or not value.is_integer())
                        )
                        for value in item[:2]
                    ):
                        return JsonResponse(
                            {
                                "success": False,
                                "error": str(
                                    _("Choose two distinct existing lesson orders.")
                                ),
                            },
                            status=400,
                        )
                    source_order = int(item[0])
                    target_order = int(item[1])
                    required_percentage = float(item[2])

                    if source_order >= target_order:
                        return JsonResponse(
                            {
                                "success": False,
                                "error": str(
                                    _("Source must be less than target in item %(num)s")
                                    % {"num": i + 1}
                                ),
                            }
                        )

                    if (
                        not math.isfinite(required_percentage)
                        or required_percentage < 0
                        or required_percentage > 100
                    ):
                        return JsonResponse(
                            {
                                "success": False,
                                "error": str(
                                    _("Percentage must be 0-100 in item %(num)s")
                                    % {"num": i + 1}
                                ),
                            }
                        )

                    # Check for duplicate pairs
                    pair_key = (source_order, target_order)
                    for j, (prev_source, prev_target, prev_pct) in enumerate(
                        validated_prereqs
                    ):
                        if (prev_source, prev_target) == pair_key:
                            return JsonResponse(
                                {
                                    "success": False,
                                    "error": str(
                                        _(
                                            "Duplicate pair [%(source)s, %(target)s] found in items %(first)s and %(second)s"
                                        )
                                        % {
                                            "source": source_order,
                                            "target": target_order,
                                            "first": j + 1,
                                            "second": i + 1,
                                        }
                                    ),
                                }
                            )

                    if counts[source_order] != 1 or counts[target_order] != 1:
                        return JsonResponse(
                            {
                                "success": False,
                                "error": str(
                                    _("Choose two distinct existing lesson orders.")
                                ),
                            },
                            status=400,
                        )

                    validated_prereqs.append(
                        (source_order, target_order, required_percentage)
                    )

                # Delete all existing prerequisites for this course
                CourseLessonPrerequisite.objects.filter(course=self.course).delete()

                CourseLessonPrerequisite.objects.bulk_create(
                    [
                        CourseLessonPrerequisite(
                            course=self.course,
                            source_order=source,
                            target_order=target,
                            required_percentage=percentage,
                        )
                        for source, target, percentage in validated_prereqs
                    ]
                )

                # Mark course for lazy recalculation for all enrolled users

                mark_course_for_recalculation(self.course)

                return JsonResponse({"success": True, "count": len(validated_prereqs)})

            except (ValueError, TypeError, OverflowError, json.JSONDecodeError) as e:
                return JsonResponse(
                    {
                        "success": False,
                        "error": str(_("Invalid JSON: %(error)s") % {"error": str(e)}),
                    }
                )

        elif action in ("reorder_lessons", "reorder_contests"):
            try:
                reorder_course_items(
                    self.course,
                    CourseLesson if action == "reorder_lessons" else CourseContest,
                    request.POST.get("order_data", "[]"),
                )
                return JsonResponse({"success": True})
            except (ValueError, TypeError, ValidationError) as exc:
                return JsonResponse({"success": False, "error": str(exc)}, status=400)

        # Handle regular form submission for lessons
        formset = self.form_class(request.POST, instance=self.course)
        problem_formsets = [
            self.get_problem_formset(post=True, lesson=lesson.instance)
            for lesson in formset.forms
            if lesson.instance.id
        ]
        for pf in problem_formsets:
            if not pf.is_valid():
                return self.form_invalid(pf)

        if formset.is_valid():
            formset.save()
            for problem_formset in problem_formsets:
                save_semantic_formset(
                    problem_formset,
                    parent_field="lesson",
                    parent=problem_formset.lesson,
                    identity_fields=("problem",),
                )
            return self.form_valid(formset)
        else:
            return self.form_invalid(formset)

    def get_success_url(self):
        return self.request.path


GRADES_PAGE_SIZE = 100


def _parse_org_filter(request):
    """Parse and validate org filter from GET params."""
    org_query = []
    if request.GET.get("orgs") and request.profile:
        try:
            org_query = list(map(int, request.GET.getlist("orgs")))
            user_org_ids = set(request.profile.get_organization_ids())
            org_query = [i for i in org_query if i in user_org_ids]
        except ValueError:
            pass
    return org_query


def _filter_students_by_org(students, org_query):
    """Filter students to those belonging to any of the selected orgs."""
    if not org_query:
        return students
    org_set = set(org_query)
    student_ids = [s.id for s in students]
    member_ids = set(
        Profile.objects.filter(id__in=student_ids, organizations__id__in=org_set)
        .distinct()
        .values_list("id", flat=True)
    )
    return [s for s in students if s.id in member_ids]


def _apply_student_filters(students, request, org_query, friend_only):
    """Apply search, org, and friend filters to a sorted student list."""
    search_query = request.GET.get("search", "").strip().lower()
    if search_query:
        students = [
            s
            for s in students
            if search_query in s.username.lower()
            or search_query in (s.first_name or "").lower()
        ]

    students = _filter_students_by_org(students, org_query)

    if friend_only:
        following_ids = set(request.profile.get_following_ids(True))
        students = [s for s in students if s.id in following_ids]

    return students


def _paginate_students(students, request, page_override=None):
    """Paginate a student list using DiggPaginator. Returns (page_students, page_obj)."""
    if not students:
        return [], None

    total = len(students)
    if page_override is not None:
        page_number = page_override
    else:
        try:
            page_number = int(request.GET.get("page", 1))
        except ValueError:
            page_number = 1
    num_pages = max(1, (total + GRADES_PAGE_SIZE - 1) // GRADES_PAGE_SIZE)
    page_number = max(1, min(page_number, num_pages))

    start = (page_number - 1) * GRADES_PAGE_SIZE
    page_students = students[start : start + GRADES_PAGE_SIZE]

    page_obj = DiggPaginator(
        students, GRADES_PAGE_SIZE, body=3, tail=1, padding=1
    ).get_page(page_number)

    return page_students, page_obj


def _grade_pagination_context(request):
    query = request.GET.copy()
    query.pop("focus", None)
    return paginate_query_context(SimpleNamespace(GET=query, path=request.path))


def _locate_student(students, username):
    """Return (1-based page, profile.id) of `username` in the already filtered+sorted
    `students` list, or (None, None) if absent."""
    if not username:
        return None, None
    for i, s in enumerate(students):
        if s.username == username:
            return i // GRADES_PAGE_SIZE + 1, s.id
    return None, None


def _get_unlocked_lessons(course, profile, is_editable):
    """Get visible lessons that are unlocked for the user."""
    all_lessons = course.get_lessons()
    if is_editable:
        return list(all_lessons)

    # Anonymous users have no profile and thus no per-user unlock state.
    # They are not course members, so the access mixin returns 404 for them
    # anyway; bail out before computing grades, which would otherwise crash in
    # bulk_calculate_lessons_progress ('NoneType' object has no attribute 'id').
    if profile is None:
        return []

    lock_status = get_lesson_lock_status(profile, course)
    return [lesson for lesson in all_lessons if not lock_status.get(lesson.id, False)]


class CourseStudentResults(CourseAccessibleMixin, DetailView):
    allow_grade_export = True
    model = Course
    template_name = "course/grades.html"

    def get_object(self):
        self.lessons = self.course.get_lessons()
        self.contests = self.course.get_contests()
        return self.course

    def get_grades(self):
        self.focus_id = None
        self.my_page = None
        self.org_query = _parse_org_filter(self.request)
        self.friend_only = (
            self.request.GET.get("friend") == "1" and self.request.profile
        )
        all_students = self.get_grade_students()

        if not all_students:
            return {}, {}, {}, None, {}

        # Keep only compact total rows for the full student set. Detailed grade
        # cells are materialized after filtering and pagination.
        lesson_progress_data = _prepare_lesson_progress_data(
            all_students, self.lessons, viewer=self.request.user
        )
        all_problem_ids = {
            problem["problem_id"]
            for problems in lesson_progress_data["lesson_problems"].values()
            for problem in problems
        }

        bulk_problem_points = bulk_max_case_points_per_problem(
            all_students, all_problem_ids, viewer=self.request.user
        )
        grade_lessons = bulk_calculate_lessons_progress(
            all_students,
            self.lessons,
            bulk_problem_points,
            progress_data=lesson_progress_data,
            include_details=False,
        )
        contest_progress_data = _prepare_contest_progress_data(
            all_students, self.contests, viewer=self.request.user
        )
        grade_contests = bulk_calculate_contests_progress(
            all_students,
            self.contests,
            progress_data=contest_progress_data,
            include_details=False,
        )

        grade_total = {}
        for student in all_students:
            grade_total[student] = calculate_total_progress(
                grade_lessons[student], grade_contests[student]
            )

        # Sort and compute global ranks with tie handling
        all_students.sort(
            key=lambda s: (-grade_total[s]["percentage"], s.username.lower())
        )
        global_rank = {}
        for rank, student in ranker(
            all_students, key=lambda s: grade_total[s]["percentage"]
        ):
            global_rank[student.id] = rank

        if (not self.course.show_classmates_grades and not self.is_editable) or any(
            value.get("is_result_hidden", False) for value in grade_total.values()
        ):
            global_rank = {}

        # Filter and paginate
        students = _apply_student_filters(
            all_students, self.request, self.org_query, self.friend_only
        )
        if not students:
            return {}, {}, {}, None, global_rank

        focus_username = self.request.GET.get("focus", "").strip() or None
        focus_page, self.focus_id = _locate_student(students, focus_username)
        if "page" in self.request.GET:
            focus_page = None
        self.my_page, _ = _locate_student(
            students, self.request.profile.username if self.request.profile else None
        )
        page_students, page_obj = _paginate_students(
            students, self.request, page_override=focus_page
        )

        page_student_ids = {student.id for student in page_students}
        page_problem_points = {
            student_id: points
            for student_id, points in bulk_problem_points.items()
            if student_id in page_student_ids
        }
        grade_lessons = bulk_calculate_lessons_progress(
            page_students,
            self.lessons,
            page_problem_points,
            progress_data=lesson_progress_data,
        )
        grade_contests = bulk_calculate_contests_progress(
            page_students,
            self.contests,
            progress_data=contest_progress_data,
        )
        grade_total = {s: grade_total[s] for s in page_students}

        return grade_lessons, grade_contests, grade_total, page_obj, global_rank

    def get_context_data(self, **kwargs):
        context = super(CourseStudentResults, self).get_context_data(**kwargs)
        context["title"] = _("Grades in %(course_name)s") % {
            "course_name": self.course.name,
        }
        context["content_title"] = mark_safe(
            _("Grades in <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": escape(self.course.name),
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "grades"
        (
            context["grade_lessons"],
            context["grade_contests"],
            context["grade_total"],
            context["page_obj"],
            context["global_rank"],
        ) = self.get_grades()
        context["focus_id"] = self.focus_id
        context["my_page"] = self.my_page
        if context["page_obj"]:
            context.update(_grade_pagination_context(self.request))
        context["search_query"] = self.request.GET.get("search", "").strip()
        context["org_query"] = self.org_query
        context["friend_only"] = self.friend_only
        if self.request.profile:
            context["organizations"] = self.request.profile.get_organizations()
        context["lesson_grade_urls"] = {
            lesson.id: reverse(
                "course_grades_lesson", args=[self.course.slug, lesson.id]
            )
            for lesson in self.lessons
        }
        context["contest_ranking_urls"] = {
            item.id: reverse("contest_ranking", args=[item.contest.key])
            for item in self.contests
        }
        context["lessons"] = self.lessons
        context["locked_lesson_ids"] = (
            set()
            if self.is_editable
            else {
                lesson_id
                for lesson_id, locked in get_lesson_lock_status(
                    self.request.profile, self.course
                ).items()
                if locked
            }
        )
        context["course_contests"] = self.contests
        return context


class CourseStudentResultsLesson(CourseAccessibleMixin, DetailView):
    allow_grade_export = True
    model = CourseLesson
    template_name = "course/grades_lesson.html"

    def get_object(self):
        try:
            self.lesson = CourseLesson.objects.get(
                course=self.course, id=self.kwargs["id"]
            )
            require_lesson_access(self.lesson, self.request.user)

            self.problems = self.lesson.get_problems()
            self.lesson_quizzes = list(
                CourseLessonQuiz.objects.filter(lesson=self.lesson, is_visible=True)
                .select_related("quiz")
                .order_by("order")
            )
            return self.lesson
        except ObjectDoesNotExist:
            raise Http404()

    def get_lesson_grades(self):
        self.focus_id = None
        self.my_page = None
        self.org_query = _parse_org_filter(self.request)
        self.friend_only = (
            self.request.GET.get("friend") == "1" and self.request.profile
        )
        all_students = self.get_grade_students()

        if not all_students:
            return {}, None, {}

        lesson_progress_data = _prepare_lesson_progress_data(
            all_students, [self.lesson], viewer=self.request.user
        )
        bulk_problem_points = bulk_max_case_points_per_problem(
            all_students, self.problems, viewer=self.request.user
        )
        grade_summaries = {
            student: _calculate_lesson_summary(
                student.id,
                self.lesson.id,
                bulk_problem_points,
                lesson_progress_data,
            )
            for student in all_students
        }

        # Sort and compute global ranks with tie handling
        all_students.sort(
            key=lambda s: (-grade_summaries[s]["percentage"], s.username.lower())
        )
        global_rank = {}
        for rank, student in ranker(
            all_students, key=lambda s: grade_summaries[s]["percentage"]
        ):
            global_rank[student.id] = rank

        if (not self.course.show_classmates_grades and not self.is_editable) or any(
            value.get("is_result_hidden", False) for value in grade_summaries.values()
        ):
            global_rank = {}

        # Filter and paginate
        students = _apply_student_filters(
            all_students, self.request, self.org_query, self.friend_only
        )
        if not students:
            return {}, None, global_rank

        focus_username = self.request.GET.get("focus", "").strip() or None
        focus_page, self.focus_id = _locate_student(students, focus_username)
        if "page" in self.request.GET:
            focus_page = None
        self.my_page, _ = _locate_student(
            students, self.request.profile.username if self.request.profile else None
        )
        page_students, page_obj = _paginate_students(
            students, self.request, page_override=focus_page
        )
        page_student_ids = {student.id for student in page_students}
        page_problem_points = {
            student_id: points
            for student_id, points in bulk_problem_points.items()
            if student_id in page_student_ids
        }
        sorted_grades = _build_lesson_grade_details(
            page_students,
            self.lesson,
            page_problem_points,
            lesson_progress_data,
        )

        return sorted_grades, page_obj, global_rank

    def get_context_data(self, **kwargs):
        context = super(CourseStudentResultsLesson, self).get_context_data(**kwargs)
        context["lesson"] = self.lesson
        context["title"] = _("Grades of %(lesson_name)s in %(course_name)s") % {
            "course_name": self.course.name,
            "lesson_name": self.lesson.title,
        }
        context["content_title"] = mark_safe(
            _(
                "Grades of <a href='%(url_lesson)s'>%(lesson_name)s</a> in <a href='%(url_course)s'>%(course_name)s</a>"
            )
            % {
                "course_name": escape(self.course.name),
                "lesson_name": escape(self.lesson.title),
                "url_course": self.course.get_absolute_url(),
                "url_lesson": self.lesson.get_absolute_url(),
            }
        )
        context["page_type"] = "grades_lesson"
        (
            context["grades"],
            context["page_obj"],
            context["global_rank"],
        ) = self.get_lesson_grades()
        context["focus_id"] = self.focus_id
        context["my_page"] = self.my_page
        if context["page_obj"]:
            context.update(_grade_pagination_context(self.request))
        context["search_query"] = self.request.GET.get("search", "").strip()
        context["org_query"] = self.org_query
        context["friend_only"] = self.friend_only
        if self.request.profile:
            context["organizations"] = self.request.profile.get_organizations()
        context["problems"] = [
            {"problem": p, "score": ps["score"]}
            for p, ps in zip(self.problems, self.lesson.get_problems_and_scores())
        ]
        context["lesson_quizzes"] = self.lesson_quizzes

        context["sidebar_lessons"] = _get_unlocked_lessons(
            self.course, self.request.profile, self.is_editable
        )
        return context


class CourseLessonUserSubmissionsAjax(CourseAccessibleMixin, View):
    """AJAX fragment: a student's submissions for a problem in a lesson.

    Rendered into the featherlight popup opened from a problem score cell on the
    lesson grades page. Permission is course-level (CourseAccessibleMixin):
    anonymous -> 401, non-member -> 403. The link to a submission's source only
    shows if the *viewer* can access it (gated in the template).
    """

    json_endpoint = True

    POPUP_LIMIT = 50

    def get(self, request, *args, **kwargs):
        lesson = get_object_or_404(
            CourseLesson, course=self.course, id=self.kwargs["id"]
        )
        require_lesson_access(lesson, request.user)

        student = get_object_or_404(Profile, user__username=self.kwargs["user"])
        self.check_grade_target(student)
        problem = get_object_or_404(Problem, code=self.kwargs["problem"])

        # problem must belong to this lesson (get_problems_and_scores -> dicts)
        lesson_problem_ids = {
            ps["problem_id"] for ps in lesson.get_problems_and_scores()
        }
        if problem.id not in lesson_problem_ids:
            raise Http404()

        # target student must be enrolled (get_students() returns a LIST, use CourseRole)
        if not CourseRole.objects.filter(course=self.course, user=student).exists():
            raise Http404()

        qs = (
            Submission.objects.filter(user=student, problem=problem)
            .select_related("language", "problem", "contest_object")
            .order_by("-id")
        )
        submissions = list(qs[: self.POPUP_LIMIT + 1])
        has_more = len(submissions) > self.POPUP_LIMIT
        submissions = submissions[: self.POPUP_LIMIT]
        mark_hidden_result_submissions(submissions, request.user)
        for s in submissions:
            s.display_point = "{} / {}".format(
                floatformat(s.points, -2), floatformat(problem.points, -2)
            )

        return render(
            request,
            "course/lesson_user_submissions_ajax.html",
            {
                "course": self.course,
                "lesson": lesson,
                "problem": problem,
                "profile": student,
                "submissions": submissions,
                "has_more": has_more,
            },
        )


class CourseLessonUserQuizAttemptsAjax(CourseAccessibleMixin, View):
    """AJAX fragment: a student's quiz attempts for a lesson quiz.

    Rendered into the featherlight popup opened from a quiz score cell on the lesson
    grades page (mirrors the contest quiz-attempts popup). Permission is course-level
    (CourseAccessibleMixin): anonymous -> 401, non-member -> 403. The link to a
    quiz result only shows if the viewer is the attempt owner or a quiz editor (the
    same gate as LessonQuizResult), so it never points at a page that would 403.
    """

    json_endpoint = True

    POPUP_LIMIT = 50

    def get(self, request, *args, **kwargs):
        lesson = get_object_or_404(
            CourseLesson, course=self.course, id=self.kwargs["id"]
        )
        require_lesson_access(lesson, request.user)

        student = get_object_or_404(Profile, user__username=self.kwargs["user"])
        self.check_grade_target(student)
        lesson_quiz = get_object_or_404(
            CourseLessonQuiz,
            id=self.kwargs["lesson_quiz_id"],
            lesson=lesson,
            is_visible=True,
        )

        # target student must be enrolled (get_students() returns a LIST, use CourseRole)
        if not CourseRole.objects.filter(course=self.course, user=student).exists():
            raise Http404()

        attempts_qs = (
            QuizAttempt.objects.filter(
                user=student, lesson_quiz=lesson_quiz, is_submitted=True
            )
            .select_related("quiz", "contest_participation__contest")
            .order_by("-end_time")
        )
        attempts = list(attempts_qs[: self.POPUP_LIMIT])

        is_result_hidden = lesson_quiz.quiz.should_hide_result(
            request.user, lesson_quiz=lesson_quiz
        )
        best_attempt = None
        if not is_result_hidden:
            best_attempt = attempts_qs.order_by("-score").first()

        can_view_results = bool(attempts) and attempts[0].is_accessible_by(request.user)

        return render(
            request,
            "course/lesson_user_quiz_attempts_ajax.html",
            {
                "course": self.course,
                "lesson": lesson,
                "lesson_quiz": lesson_quiz,
                "profile": student,
                "attempts": attempts,
                "best_attempt": best_attempt,
                "can_view_results": can_view_results,
                "is_result_hidden": is_result_hidden,
            },
        )


class AddCourseContestForm(forms.ModelForm):
    points = forms.IntegerField(label=_("Points"))

    class Meta:
        model = Contest
        fields = [
            "points",
            "key",
            "name",
            "start_time",
            "end_time",
            "problems",
        ]
        widgets = {
            "start_time": DateTimePickerWidget(),
            "end_time": DateTimePickerWidget(),
            "problems": HeavySelect2MultipleWidget(data_view="problem_select2"),
        }

    def save(self, course, profile, commit=True):
        contest = super().save(commit=False)
        contest.is_in_course = True

        old_save_m2m = self.save_m2m

        def save_m2m():
            for i, problem in enumerate(self.cleaned_data["problems"]):
                contest_problem = ContestProblem(
                    contest=contest, problem=problem, points=100, order=i + 1
                )
                contest_problem.save()
                contest.contest_problems.add(contest_problem)
            contest.authors.add(profile)
            old_save_m2m()

        self.save_m2m = save_m2m
        contest.save()
        self.save_m2m()

        # Auto-assign next order
        max_order = (
            CourseContest.objects.filter(course=course).aggregate(
                max_order=Max("order")
            )["max_order"]
            or 0
        )
        CourseContest.objects.create(
            course=course,
            contest=contest,
            order=max_order + 1,
            points=self.cleaned_data["points"],
        )

        return contest


class AddCourseContest(CourseEditableMixin, FormView):
    template_name = "course/add_contest.html"
    form_class = AddCourseContestForm

    def get_title(self):
        return _("Add contest")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = self.get_title()
        return context

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Added from course") + " " + self.course.name)
            revisions.set_user(self.request.user)

            self.contest = form.save(course=self.course, profile=self.request.profile)

        return super().form_valid(form)

    def get_success_url(self):
        return reverse("contest_edit", args=[self.contest.key])


class CourseContestList(CourseEditableMixin, ListView):
    template_name = "course/contest_list.html"
    context_object_name = "course_contests"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Contest list")
        context["content_title"] = mark_safe(
            _("Edit contests for <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": escape(self.course.name),
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "contests"
        context["tab"] = self.request.GET.get("tab", "contests")
        return context

    def get_queryset(self):
        return self.course.contests.select_related("contest").all().order_by("order")

    def post(self, request, *args, **kwargs):
        action = request.POST.get("action")

        if action == "reorder_contests":
            try:
                reorder_course_items(
                    self.course, CourseContest, request.POST.get("order_data", "[]")
                )
                return JsonResponse({"success": True})
            except (ValueError, TypeError, ValidationError) as exc:
                return JsonResponse({"success": False, "error": str(exc)}, status=400)

        if action == "update_points":
            cc_id = request.POST.get("cc_id")
            points_raw = request.POST.get("points")
            try:
                cc_id = int(cc_id)
                points = int(points_raw)
                if points < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return JsonResponse(
                    {"success": False, "error": str(_("Invalid points value."))}
                )
            cc = get_object_or_404(CourseContest, id=cc_id, course=self.course)
            cc.points = points
            cc.save(update_fields=["points"])
            return JsonResponse({"success": True, "points": points})

        return JsonResponse({"success": False, "error": str(_("Unknown action."))})


class CourseContestEditRedirect(View):
    """
    Backward-compat redirect: course-scoped contest editing was consolidated
    into the standalone /contest/<key>/edit page. Permission is enforced by
    ContestEdit.dispatch (course teacher/assistant path).
    """

    def get(self, request, slug, contest, *args, **kwargs):
        return HttpResponseRedirect(reverse("contest_edit", args=[contest]))


def is_lesson_clonable(request, lesson):
    # Admins can clone any lesson
    if request.user.is_superuser:
        return True

    # User must be able to edit the source course
    if not Course.is_editable_by(lesson.course, request.profile):
        return False

    # User must have at least one other course they can edit
    other_editable_courses = Course.objects.filter(
        courserole__user=request.profile,
        courserole__role__in=EDITABLE_ROLES,
    )

    return other_editable_courses.exists()


class CourseSelect2View(View):
    def get(self, request, *args, **kwargs):

        term = request.GET.get("term", "")

        # Admins can see all courses
        if request.user.is_superuser:
            queryset = Course.objects.all()
        else:
            # Only show courses the user can edit
            queryset = Course.objects.filter(
                courserole__user=request.profile,
                courserole__role__in=EDITABLE_ROLES,
            )

        if term:
            queryset = queryset.filter(name__icontains=term)

        results = []
        for course in queryset[:20]:  # Limit to 20 results
            results.append(
                {
                    "id": course.slug,
                    "text": smart_str(course.name),
                }
            )

        return JsonResponse(
            {
                "results": results,
                "more": False,
            }
        )


class LessonClone(CourseEditableMixin, TitleMixin, SingleObjectFormView):
    title = _("Clone Lesson")
    template_name = "course/clone.html"
    form_class = LessonCloneForm
    model = CourseLesson

    def get_object(self, queryset=None):
        try:
            lesson = CourseLesson.objects.get(
                course=self.course, id=self.kwargs["lesson_id"]
            )
            if not is_lesson_clonable(self.request, lesson):
                raise Http404()
            return lesson
        except ObjectDoesNotExist:
            raise Http404()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["profile"] = self.request.profile
        return kwargs

    def form_valid(self, form):
        target_course = form.cleaned_data["course"]
        new_title = form.cleaned_data["title"]

        with revisions.create_revision():
            # Store lesson problems before cloning
            lesson_problems = list(self.object.lesson_problems.all())

            # Deep copy the lesson
            cloned_lesson = deepcopy(self.object)
            cloned_lesson.pk = None  # New ID will be auto-generated
            cloned_lesson.course = target_course
            cloned_lesson.title = new_title  # Use new title from form
            cloned_lesson.is_visible = False  # Public visibility off
            cloned_lesson.order = (  # Calculate next order
                target_course.lessons.aggregate(Max("order"))["order__max"] or 0
            ) + 1
            cloned_lesson.save()

            # Copy all lesson problems
            cloned_problems = []
            for lesson_problem in lesson_problems:
                cloned_problem = deepcopy(lesson_problem)
                cloned_problem.pk = None  # New ID will be auto-generated
                cloned_problem.lesson = cloned_lesson
                cloned_problems.append(cloned_problem)

            if cloned_problems:
                CourseLessonProblem.objects.bulk_create(cloned_problems)

            CourseLessonQuiz.objects.bulk_create(
                [
                    CourseLessonQuiz(
                        lesson=cloned_lesson,
                        quiz_id=link.quiz_id,
                        order=link.order,
                        points=link.points,
                        max_attempts=link.max_attempts,
                        is_visible=link.is_visible,
                        is_result_hidden=link.is_result_hidden,
                    )
                    for link in self.object.lesson_quizzes.all()
                ]
            )
            # Add revision tracking details
            revisions.set_comment(
                _("Cloned lesson '{}' to course {}").format(
                    new_title, target_course.name
                )
            )
            revisions.set_user(self.request.user)

        # Redirect to edit the cloned lesson in target course
        return HttpResponseRedirect(
            reverse(
                "edit_course_lessons_new", args=[target_course.slug, cloned_lesson.id]
            )
        )


class CourseMemberForm(forms.Form):
    users = forms.ModelMultipleChoiceField(
        queryset=Profile.objects.select_related("user"),
        required=False,
        widget=HeavySelect2MultipleWidget(data_view="profile_select2"),
        label=_("Select users"),
    )
    usernames = forms.CharField(
        required=False,
        max_length=32000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label=_("Paste usernames"),
        help_text=_(
            "Separate usernames with spaces, commas, or new lines. Maximum 200 users per batch."
        ),
    )
    role = forms.ChoiceField(
        choices=RoleInCourse.choices, initial=RoleInCourse.STUDENT, label=_("Role")
    )
    confirmation = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, course=None, current_user_role=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.course = course
        self.current_user_role = current_user_role
        if current_user_role == RoleInCourse.ASSISTANT:
            self.fields["role"].choices = [
                (RoleInCourse.STUDENT, _("Student")),
                (RoleInCourse.ASSISTANT, _("Assistant")),
            ]

    def clean(self):
        cleaned = super().clean()
        profiles = {profile.pk: profile for profile in cleaned.get("users", [])}
        names = {}
        for profile in profiles.values():
            names.setdefault(profile.user.username.casefold(), profile.user.username)
        for name in re.split(r"[\s,]+", cleaned.get("usernames", "").strip()):
            if name:
                names.setdefault(name.casefold(), name)
        if len(names) > 200:
            raise ValidationError(_("Add at most 200 users at a time."))
        found, unknown = resolve_member_usernames(list(names.values()))
        if unknown:
            raise ValidationError(
                _("Unknown usernames: %(users)s") % {"users": ", ".join(unknown)}
            )
        profiles.update({profile.pk: profile for profile in found})
        if not profiles:
            raise ValidationError(_("Select users or paste usernames to add."))
        cleaned["profiles"] = list(Profile.get_cached_instances(*sorted(profiles)))
        return cleaned


class CourseMembers(CourseAdminMixin, FormView):
    template_name = "course/members.html"
    form_class = CourseMemberForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs.update(
            course=self.course, current_user_role=self.get_user_role_in_course()
        )
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        members = CourseRole.objects.filter(course=self.course)
        context["member_count"] = members.count()
        search = self.request.GET.get("search", "").strip()
        role_filter = self.request.GET.get("role", "")
        if search:
            members = members.filter(
                Q(user__user__username__icontains=search)
                | Q(user__user__first_name__icontains=search)
            )
        if role_filter in RoleInCourse.values:
            members = members.filter(role=role_filter)
        members = members.annotate(
            role_priority=Case(
                When(role=RoleInCourse.TEACHER, then=Value(1)),
                When(role=RoleInCourse.ASSISTANT, then=Value(2)),
                default=Value(3),
                output_field=IntegerField(),
            )
        ).order_by("role_priority", "user__user__username", "pk")
        page = DiggPaginator(members, 50, body=3, tail=1, padding=1).get_page(
            self.request.GET.get("page", 1)
        )
        rows = list(page.object_list)
        profiles = {
            profile.pk: profile
            for profile in Profile.get_cached_instances(*[row.user_id for row in rows])
        }
        for row in rows:
            row.user = profiles[row.user_id]
        context.update(
            members=rows,
            page_obj=page,
            search_query=search,
            member_role_filter=role_filter,
            title=_("Members"),
            content_title=_("Members"),
            page_type="members",
            role_choices=RoleInCourse.choices,
            current_user_role=self.get_user_role_in_course(),
        )
        context.update(paginate_query_context(self.request))
        return context

    def preview_payload(self, form):
        ids = sorted(profile.pk for profile in form.cleaned_data["profiles"])
        existing = list(
            CourseRole.objects.filter(course=self.course, user_id__in=ids)
            .order_by("user_id")
            .values_list("user_id", "role")
        )
        return {
            "course": self.course.pk,
            "actor": self.request.profile.pk,
            "actor_role": self.get_user_role_in_course(),
            "ids": ids,
            "role": form.cleaned_data["role"],
            "existing": [list(row) for row in existing],
        }

    def form_valid(self, form):
        with transaction.atomic():
            Course.objects.select_for_update().get(pk=self.course.pk)
            self.course_role = (
                CourseRole.objects.filter(course=self.course, user=self.request.profile)
                .values_list("role", flat=True)
                .first()
            )
            if not self.user_can_assign_role(form.cleaned_data["role"]):
                raise PermissionDenied()
            payload = self.preview_payload(form)
            token = form.cleaned_data.get("confirmation")
            if self.request.POST.get("action") == "confirm":
                try:
                    if (
                        signing.loads(token, salt="course-members", max_age=900)
                        != payload
                    ):
                        raise signing.BadSignature()
                except signing.BadSignature:
                    form.add_error(
                        None,
                        _(
                            "The selection or permissions changed. Preview the batch again."
                        ),
                    )
                    return self.form_invalid(form)
                existing = {row[0] for row in payload["existing"]}
                new = [
                    CourseRole(course=self.course, user_id=pk, role=payload["role"])
                    for pk in payload["ids"]
                    if pk not in existing
                ]
                with revisions.create_revision():
                    CourseRole.objects.bulk_create(new)
                    created_rows = CourseRole.objects.filter(
                        course=self.course, user_id__in=[row.user_id for row in new]
                    )
                    for row in created_rows:
                        revisions.add_to_revision(row)
                    revisions.set_user(self.request.user)
                    revisions.set_comment(_("Added course members"))
                get_course_role_profile_ids.dirty(self.course.pk, payload["role"])
                transaction.on_commit(
                    partial(
                        get_course_role_profile_ids.dirty,
                        self.course.pk,
                        payload["role"],
                    )
                )
                messages.success(
                    self.request,
                    _("Added %(added)d members; skipped %(existing)d existing members.")
                    % {"added": len(new), "existing": len(existing)},
                )
                return redirect(self.get_success_url())
            existing = {row[0] for row in payload["existing"]}
            preview = [
                {"profile": profile, "existing": profile.pk in existing}
                for profile in form.cleaned_data["profiles"]
            ]
            return self.render_to_response(
                self.get_context_data(
                    form=form,
                    batch_preview=preview,
                    batch_confirmation=signing.dumps(payload, salt="course-members"),
                )
            )

    def get_success_url(self):
        return reverse("course_members", args=[self.course.slug]) + (
            "?" + self.request.GET.urlencode() if self.request.GET else ""
        )


class CourseRemoveMember(CourseAdminMixin, View):
    def post(self, request, *args, **kwargs):
        member_id = request.POST.get("member_id")

        try:
            course_role = CourseRole.objects.get(id=member_id, course=self.course)
            target_role = course_role.role

            # Check permissions for member removal
            if not self.user_can_manage_user(target_role):
                messages.error(
                    request, _("You do not have permission to remove this user.")
                )
                return HttpResponseRedirect(
                    reverse("course_members", args=[self.course.slug])
                    + ("?" + request.GET.urlencode() if request.GET else "")
                )

            with revisions.create_revision():
                # Add revision tracking details before deletion
                revisions.set_comment(
                    _("Removed member '{}' with role {} from course {}").format(
                        course_role.user.user.username,
                        course_role.get_role_display(),
                        self.course.name,
                    )
                )
                revisions.set_user(request.user)

                course_role.delete()

            messages.success(request, _("Member removed successfully."))
        except (CourseRole.DoesNotExist, ValueError, TypeError):
            messages.error(request, _("Member not found."))

        return HttpResponseRedirect(
            reverse("course_members", args=[self.course.slug])
            + ("?" + request.GET.urlencode() if request.GET else "")
        )


class CourseUpdateMemberRole(CourseAdminMixin, View):
    def post(self, request, *args, **kwargs):
        member_id = request.POST.get("member_id")
        new_role = request.POST.get("role")

        if new_role not in [choice[0] for choice in RoleInCourse.choices]:
            messages.error(request, _("Invalid role selected."))
            return HttpResponseRedirect(
                reverse("course_members", args=[self.course.slug])
                + ("?" + request.GET.urlencode() if request.GET else "")
            )

        try:
            course_role = CourseRole.objects.get(id=member_id, course=self.course)
            old_role = course_role.role

            # Check permissions for role changes
            # First check if user can manage the target user
            if not self.user_can_manage_user(old_role):
                messages.error(
                    request, _("You do not have permission to manage this user.")
                )
                return HttpResponseRedirect(
                    reverse("course_members", args=[self.course.slug])
                    + ("?" + request.GET.urlencode() if request.GET else "")
                )

            # Then check if user can assign the new role
            if not self.user_can_assign_role(new_role):
                messages.error(
                    request, _("You do not have permission to assign this role.")
                )
                return HttpResponseRedirect(
                    reverse("course_members", args=[self.course.slug])
                    + ("?" + request.GET.urlencode() if request.GET else "")
                )

            with revisions.create_revision():
                old_role_display = course_role.get_role_display()
                course_role.role = new_role
                course_role.save()

                # Add revision tracking details
                revisions.set_comment(
                    _("Updated member '{}'s role from {} to {} in course {}").format(
                        course_role.user.user.username,
                        old_role_display,
                        CourseRole(role=new_role).get_role_display(),
                        self.course.name,
                    )
                )
                revisions.set_user(request.user)

            new_role_display = CourseRole(role=new_role).get_role_display()
            messages.success(
                request,
                _("Updated %(user)s's role from %(old_role)s to %(new_role)s.")
                % {
                    "user": course_role.user.user.username,
                    "old_role": old_role_display,
                    "new_role": new_role_display,
                },
            )
        except (CourseRole.DoesNotExist, ValueError, TypeError):
            messages.error(request, _("Member not found."))

        return HttpResponseRedirect(
            reverse("course_members", args=[self.course.slug])
            + ("?" + request.GET.urlencode() if request.GET else "")
        )


class CourseEditForm(DirectUploadFormMixin, forms.ModelForm):
    organizations = forms.ModelMultipleChoiceField(
        queryset=Organization.objects.none(),
        required=False,
        widget=HeavySelect2MultipleWidget(
            data_view="organization_select2", attrs={"style": "width: 100%"}
        ),
        label=_("Organizations"),
        help_text=_(
            "Select organizations for this course. Leave empty for public course."
        ),
    )

    # Read-only field for displaying current organizations to non-superusers
    organizations_display = forms.CharField(
        required=False,
        widget=HTMLDisplayWidget(),
        label=_("Organizations"),
        help_text=_("Current organizations for this course."),
    )

    # Read-only display fields for TAs
    slug_display = forms.CharField(
        required=False,
        widget=HTMLDisplayWidget(),
        label=_("Course Slug"),
        help_text=_("Course name shown in URL (read-only for assistants)."),
    )

    is_public_display = forms.CharField(
        required=False,
        widget=HTMLDisplayWidget(),
        label=_("Publicly Visible"),
        help_text=_(
            "Whether this course is visible to all users (read-only for assistants)."
        ),
    )

    is_open_display = forms.CharField(
        required=False,
        widget=HTMLDisplayWidget(),
        label=_("Public Registration"),
        help_text=_("Whether users can join this course (read-only for assistants)."),
    )

    class Meta:
        model = Course
        fields = [
            "name",
            "about",
            "is_public",
            "is_open",
            "show_classmates_grades",
            "slug",
            "course_image",
            "organizations",
            "organizations_display",
            "slug_display",
            "is_public_display",
            "is_open_display",
        ]
        widgets = {
            "name": forms.TextInput(attrs={"style": "width: 100%"}),
            "about": HeavyPreviewPageDownWidget(preview=reverse_lazy("blog_preview")),
            "slug": forms.TextInput(attrs={"style": "width: 100%"}),
            "course_image": DirectUploadImageWidget(
                upload_to="course_images",
                prefix="course",
            ),
        }
        labels = {
            "name": _("Course Name"),
            "about": _("Course Description"),
            "is_public": _("Publicly Visible"),
            "is_open": _("Public Registration"),
            "slug": _("Course Slug"),
            "course_image": _("Course Image"),
        }
        help_texts = {
            "name": _("Required. Maximum 128 characters."),
            "about": _("Optional. Detailed description of the course."),
            "slug": _(
                "Required. Course name shown in URL. Only alphanumeric characters and hyphens."
            ),
            "is_public": _("Whether this course is visible to all users"),
            "is_open": _("If checked, users can join this course"),
            "course_image": _(
                "Optional. Upload an image for the course (maximum 5MB)."
            ),
        }

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop("request", None)
        self.user_role = kwargs.pop("user_role", None)
        super().__init__(*args, **kwargs)
        self.fields["about"].required = False

        # Check if user is superuser to determine field visibility
        is_superuser = self.request and self.request.user.is_superuser
        is_assistant = self.user_role == RoleInCourse.ASSISTANT

        if is_assistant and not is_superuser:
            self.fields["show_classmates_grades"].disabled = True

        # Handle organizations field (superuser only)
        if is_superuser:
            # Superusers can edit organizations - hide the display field
            del self.fields["organizations_display"]

            # Set organizations queryset for superusers (all organizations)
            self.fields["organizations"].queryset = Organization.objects.all()
        else:
            # Non-superusers cannot edit organizations - hide the editable field
            del self.fields["organizations"]

            # Set up the read-only display field with clickable links
            if self.instance and self.instance.pk:

                organizations = self.instance.organizations.all()
                if organizations:
                    org_links = []
                    for org in organizations:
                        org_url = reverse("organization_home", args=[org.pk, org.slug])
                        org_links.append(
                            format_html(
                                '<a href="{}" target="_blank">{}</a>', org_url, org.name
                            )
                        )
                    self.fields["organizations_display"].initial = format_html(
                        ", ".join(org_links)
                    )
                else:
                    self.fields["organizations_display"].initial = _("No organizations")
            else:
                self.fields["organizations_display"].initial = _("No organizations")

        # Handle role-based field restrictions for Teachers vs Assistants
        if is_assistant and not is_superuser:
            # TAs cannot edit these administrative fields - replace with read-only versions

            # Remove editable fields
            del self.fields["slug"]
            del self.fields["is_public"]
            del self.fields["is_open"]

            # Set up read-only display fields
            if self.instance and self.instance.pk:
                self.fields["slug_display"].initial = self.instance.slug
                self.fields["is_public_display"].initial = (
                    _("Yes") if self.instance.is_public else _("No")
                )
                self.fields["is_open_display"].initial = (
                    _("Yes") if self.instance.is_open else _("No")
                )
            else:
                self.fields["slug_display"].initial = _("Not set")
                self.fields["is_public_display"].initial = _("No")
                self.fields["is_open_display"].initial = _("No")
        else:
            # Teachers and superusers can edit all fields - remove display fields
            del self.fields["slug_display"]
            del self.fields["is_public_display"]
            del self.fields["is_open_display"]

        # Make required fields as specified by user
        self.fields["name"].required = True
        self.fields["about"].required = False  # Description is optional
        if "slug" in self.fields:
            self.fields["slug"].required = True
        self.fields["course_image"].required = False  # Image is optional

    def clean(self):
        cleaned_data = super().clean()

        # Validate that non-superusers cannot modify organizations
        if self.request and not self.request.user.is_superuser:
            # For non-superusers, organizations should not be in cleaned_data
            # since the field is removed in __init__
            if "organizations" in cleaned_data:
                raise forms.ValidationError(
                    _("You do not have permission to modify course organizations.")
                )

        return cleaned_data

    def save(self, commit=True):
        course = super().save(commit=commit)
        if commit:
            # Only allow superusers to modify organizations
            if self.request and self.request.user.is_superuser:
                # Handle organizations assignment for superusers
                organizations = self.cleaned_data.get("organizations", [])
                if organizations:
                    course.organizations.set(organizations)
                else:
                    course.organizations.clear()
            # For non-superusers, organizations remain unchanged
            course.save()
        return course

    def clean_slug(self):
        slug = self.cleaned_data.get("slug")
        if slug:
            # Check for slug uniqueness, excluding current instance if editing
            queryset = Course.objects.filter(slug=slug)
            if self.instance and self.instance.pk:
                queryset = queryset.exclude(pk=self.instance.pk)

            if queryset.exists():
                raise forms.ValidationError(
                    _("A course with this slug already exists.")
                )

        return slug

    def clean_name(self):
        name = self.cleaned_data.get("name")
        if name and len(name) > 128:
            raise forms.ValidationError(_("Course name cannot exceed 128 characters."))
        return name

    def clean_course_image(self):
        course_image = self.cleaned_data.get("course_image")
        if course_image and isinstance(course_image, UploadedFile):
            if course_image.size > 5 * 1024 * 1024:  # 5MB limit
                raise forms.ValidationError(
                    _("File size exceeds the maximum allowed limit of 5MB.")
                )
        return course_image


class CourseEdit(CourseEditableMixin, SingleObjectFormView):
    title = _("Edit Course")
    template_name = "course/edit.html"
    form_class = CourseEditForm
    model = Course

    def get_object(self, queryset=None):
        return self.course

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.course
        kwargs["request"] = self.request
        kwargs["user_role"] = self.get_user_role_in_course()
        kwargs["profile"] = self.request.profile
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit %(course_name)s") % {"course_name": self.course.name}
        context["content_title"] = mark_safe(
            _('Edit <a href="%(url)s">%(course_name)s</a>')
            % {
                "course_name": escape(self.course.name),
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "edit"
        return context

    def form_valid(self, form):
        with revisions.create_revision():
            self.object = form.save()

            # Add revision tracking details
            revisions.set_comment(
                _("Updated course details for {}").format(self.course.name)
            )
            revisions.set_user(self.request.user)

            messages.success(self.request, _("Course updated successfully."))
            return super().form_valid(form)

    def get_success_url(self):
        return reverse("course_edit", args=[self.object.slug])


class CourseJoin(LoginRequiredMixin, View):
    """View to handle joining a course"""

    @transaction.atomic
    def post(self, request, slug):
        course = get_object_or_404(Course.objects.select_for_update(), slug=slug)
        profile = request.profile

        if CourseRole.objects.filter(course=course, user=profile).exists():
            return redirect("course_detail", slug=course.slug)

        # Check if user can join this course
        if not Course.is_joinable(course, profile):
            messages.error(request, _("You cannot join this course."))
            return redirect("course_list")

        # Join the course as a student
        with revisions.create_revision():
            CourseRole.objects.get_or_create(
                course=course, user=profile, defaults={"role": RoleInCourse.STUDENT}
            )

            revisions.set_comment(
                _("User '{}' joined course '{}'").format(
                    profile.user.username, course.name
                )
            )
            revisions.set_user(request.user)

        messages.success(
            request, _("Successfully joined course: {}").format(course.name)
        )
        return redirect("course_detail", slug=course.slug)

    def get(self, request, slug):
        # Redirect GET requests to course list
        return redirect("course_list")


class CourseLeave(LoginRequiredMixin, View):
    """View to handle leaving a course"""

    @transaction.atomic
    def post(self, request, slug):
        course = get_object_or_404(Course.objects.select_for_update(), slug=slug)
        profile = request.profile

        # Check if user is enrolled in this course
        try:
            course_role = CourseRole.objects.get(course=course, user=profile)
        except CourseRole.DoesNotExist:
            messages.error(request, _("You are not enrolled in this course."))
            return redirect("course_detail", slug=course.slug)

        # Prevent teachers and assistants from leaving (they need to be removed by admin)
        if course_role.role in [RoleInCourse.TEACHER, RoleInCourse.ASSISTANT]:
            messages.error(
                request,
                _(
                    "Teachers and assistants cannot leave the course. Please contact an administrator."
                ),
            )
            return redirect("course_detail", slug=course.slug)

        # Only students can leave
        if course_role.role == RoleInCourse.STUDENT:
            with revisions.create_revision():
                revisions.set_comment(
                    _("User '{}' left course '{}'").format(
                        profile.user.username, course.name
                    )
                )
                revisions.set_user(request.user)

                course_role.delete()

            messages.success(
                request, _("Successfully left course: {}").format(course.name)
            )
            return redirect("course_list")

        messages.error(request, _("Unable to leave course."))
        return redirect("course_detail", slug=course.slug)

    def get(self, request, slug):
        # Redirect GET requests to course detail
        return redirect("course_detail", slug=slug)


class CourseRefreshProgress(CourseEditableMixin, View):
    """
    View for Teachers/TAs to trigger progress recalculation for all students.
    Marks all enrolled students' needs_progress_recalculation flag to True.
    """

    def post(self, request, slug):

        mark_course_for_recalculation(self.course)
        messages.success(
            request,
            _("Progress recalculation scheduled for all students in this course."),
        )
        return redirect("edit_course_lessons", slug=self.course.slug)

    def get(self, request, slug):
        # Redirect GET requests to lessons edit page
        return redirect("edit_course_lessons", slug=slug)
