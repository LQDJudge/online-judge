from copy import deepcopy
from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Max, F, Sum, Q
from django.forms import (
    inlineformset_factory,
    ModelForm,
    modelformset_factory,
)
from django.core.files.uploadedfile import UploadedFile
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.utils.html import mark_safe
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, DetailView, View, CreateView
from django.views.generic.edit import FormView
from reversion import revisions

from judge.forms import (
    ContestProblemFormSet,
    ContestQuizFormSet,
    LessonCloneForm,
    AddCourseForm,
)
from judge.models import (
    Course,
    Contest,
    CourseLesson,
    Submission,
    Profile,
    CourseLessonProblem,
    CourseContest,
    ContestProblem,
    ContestParticipation,
    CourseRole,
    Organization,
    CourseLessonQuiz,
    QuizAttempt,
)
from judge.models.course import RoleInCourse, EDITABLE_ROLES
from judge.utils.contest import (
    maybe_trigger_contest_rescore,
)
from judge.utils.problems import (
    user_attempted_ids,
    user_completed_ids,
)
from judge.widgets import (
    HeavyPreviewPageDownWidget,
    HeavySelect2MultipleWidget,
    HeavySelect2Widget,
    DateTimePickerWidget,
    Select2MultipleWidget,
    Select2Widget,
    ImageWidget,
)
from judge.forms import HTMLDisplayWidget
from judge.utils.views import SingleObjectFormView, TitleMixin, DiggPaginatorMixin


def bulk_max_case_points_per_problem(students, all_problems):
    """
    Fetch max case points for all students and problems in one query.
    Returns nested dict for O(1) lookups: {user_id: {problem_id: {case_points, case_total}}}
    Gets the submission with the highest percentage (case_points/case_total) when case_total > 0.
    """
    if not students or not all_problems:
        return {}

    # Get all submissions and find the best percentage for each user-problem pair
    submissions = Submission.objects.filter(
        user__in=students, problem__in=all_problems, case_total__gt=0
    ).values("user", "problem", "case_points", "case_total")

    # Build nested dict and keep only the best submission per user-problem
    result = {}
    for sub in submissions:
        user_id = sub["user"]
        problem_id = sub["problem"]

        percentage = (
            sub["case_points"] / sub["case_total"] if sub["case_total"] > 0 else 0
        )

        if user_id not in result:
            result[user_id] = {}

        if problem_id in result[user_id]:
            existing = result[user_id][problem_id]
            existing_percentage = (
                existing["case_points"] / existing["case_total"]
                if existing["case_total"] > 0
                else 0
            )

            if percentage > existing_percentage:
                result[user_id][problem_id] = {
                    "case_points": sub["case_points"],
                    "case_total": sub["case_total"],
                }
        else:
            result[user_id][problem_id] = {
                "case_points": sub["case_points"],
                "case_total": sub["case_total"],
            }

    return result


def bulk_calculate_lessons_progress(students, lessons, bulk_problem_points):
    """
    Calculate progress for all students and lessons using pre-fetched data.
    Includes both problem scores and quiz scores.

    Args:
        students: List of Profile objects
        lessons: QuerySet of CourseLesson objects (should be prefetched)
        bulk_problem_points: Dict from bulk_max_case_points_per_problem()

    Returns:
        Dict: {student: lesson_progress_dict}
    """
    results = {}

    # Pre-fetch all lesson problems data to avoid repeated queries
    lesson_problems_data = {}
    for lesson in lessons:
        lesson_problems_data[lesson.id] = [
            {"problem_id": p["problem_id"], "score": p["score"]}
            for p in lesson.get_problems_and_scores()
        ]

    # Pre-fetch all lesson quizzes data with quiz objects for get_total_points()
    lesson_quizzes_data = {}
    quiz_totals = {}  # Map quiz_id -> current total points
    for lesson in lessons:
        lesson_quiz_objects = list(
            CourseLessonQuiz.objects.filter(
                lesson=lesson, is_visible=True
            ).select_related("quiz")
        )
        lesson_quizzes_data[lesson.id] = [
            {"id": lq.id, "quiz_id": lq.quiz_id, "points": lq.points}
            for lq in lesson_quiz_objects
        ]
        # Store current quiz totals
        for lq in lesson_quiz_objects:
            if lq.quiz_id not in quiz_totals:
                quiz_totals[lq.quiz_id] = lq.quiz.get_total_points()

    # Bulk fetch best quiz attempts for all students and lessons
    # Build a dict of {(student_id, lesson_quiz_id): best_score_ratio}
    # Include ALL attempts for a quiz (not just lesson-linked ones)
    student_ids = [s.id for s in students]
    lesson_quiz_ids = []
    quiz_ids = set()
    lesson_quiz_to_quiz = {}  # Map lesson_quiz_id -> quiz_id

    for lesson_id, quizzes in lesson_quizzes_data.items():
        for quiz_data in quizzes:
            lesson_quiz_ids.append(quiz_data["id"])
            quiz_ids.add(quiz_data["quiz_id"])
            lesson_quiz_to_quiz[quiz_data["id"]] = quiz_data["quiz_id"]

    # Get best attempts for all students and lesson quizzes
    best_quiz_scores = {}
    if quiz_ids and student_ids:
        from django.db.models import Max

        # Get best score for each (user, quiz) combination - include ALL attempts
        # regardless of whether they came from a lesson or direct quiz access
        best_attempts = (
            QuizAttempt.objects.filter(
                user_id__in=student_ids,
                quiz_id__in=quiz_ids,
                is_submitted=True,
            )
            .values("user_id", "quiz_id")
            .annotate(
                best_score=Max("score"),
            )
        )

        # Build a dict of {(student_id, quiz_id): best_score_ratio}
        # Use current quiz totals instead of max_score from attempts
        quiz_best_scores = {}
        for attempt in best_attempts:
            quiz_id = attempt["quiz_id"]
            quiz_max = quiz_totals.get(quiz_id, 0)
            if quiz_max and quiz_max > 0:
                ratio = float(attempt["best_score"] or 0) / float(quiz_max)
            else:
                ratio = 0
            quiz_best_scores[(attempt["user_id"], quiz_id)] = ratio

        # Map back to lesson_quiz_id for the grade calculation
        for lesson_quiz_id, quiz_id in lesson_quiz_to_quiz.items():
            for student_id in student_ids:
                if (student_id, quiz_id) in quiz_best_scores:
                    best_quiz_scores[(student_id, lesson_quiz_id)] = quiz_best_scores[
                        (student_id, quiz_id)
                    ]

    for student in students:
        student_results = {}
        total_achieved_points = total_lesson_points = 0

        for lesson in lessons:
            achieved_points = total_points = 0

            # Calculate problem points
            student_points = bulk_problem_points.get(student.id, {})

            for lp_data in lesson_problems_data[lesson.id]:
                problem_data = student_points.get(lp_data["problem_id"])
                if problem_data and problem_data["case_total"]:
                    achieved_points += (
                        problem_data["case_points"]
                        / problem_data["case_total"]
                        * lp_data["score"]
                    )
                total_points += lp_data["score"]

            # Calculate quiz points
            for quiz_data in lesson_quizzes_data[lesson.id]:
                quiz_points = quiz_data["points"] or 0
                total_points += quiz_points

                # Get best score ratio for this student and lesson quiz
                score_ratio = best_quiz_scores.get((student.id, quiz_data["id"]), 0)
                achieved_points += score_ratio * quiz_points

            student_results[lesson.id] = {
                "achieved_points": achieved_points,
                "total_points": total_points,
                "percentage": (
                    achieved_points / total_points * 100 if total_points else 0
                ),
            }

            if total_points:
                total_achieved_points += achieved_points / total_points * lesson.points
            total_lesson_points += lesson.points

        student_results["total"] = {
            "achieved_points": total_achieved_points,
            "total_points": total_lesson_points,
            "percentage": (
                total_achieved_points / total_lesson_points * 100
                if total_lesson_points
                else 0
            ),
        }
        results[student] = student_results

    return results


def bulk_calculate_contests_progress(students, course_contests):
    """
    Calculate contest progress for all students using bulk queries.
    Returns nested dict: {student: contest_progress_dict}
    """
    if not students or not course_contests:
        return {
            student: {
                "total": {"achieved_points": 0, "total_points": 0, "percentage": 0}
            }
            for student in students
        }

    # Get all contests from course_contests
    contests = [cc.contest for cc in course_contests]

    # Bulk query for all participations
    participations = ContestParticipation.objects.filter(
        contest__in=contests, user__in=students, virtual=0
    ).values("contest", "user", "score")

    # Build participation lookup: {(contest_id, user_id): score}
    participation_lookup = {}
    for p in participations:
        participation_lookup[(p["contest"], p["user"])] = p["score"]

    # Bulk query for contest total points
    contest_totals = (
        ContestProblem.objects.filter(contest__in=contests)
        .values("contest")
        .annotate(total_points=Sum("points"))
    )

    # Build contest totals lookup: {contest_id: total_points}
    contest_points_lookup = {}
    for ct in contest_totals:
        contest_points_lookup[ct["contest"]] = ct["total_points"] or 0

    # Calculate progress for all students
    results = {}
    for student in students:
        student_results = {}
        total_achieved_points = total_contest_points = 0

        for course_contest in course_contests:
            contest = course_contest.contest
            achieved_points = participation_lookup.get((contest.id, student.id), 0)
            total_points = contest_points_lookup.get(contest.id, 0)

            student_results[course_contest.id] = {
                "achieved_points": achieved_points,
                "total_points": total_points,
                "percentage": (
                    achieved_points / total_points * 100 if total_points else 0
                ),
            }

            if total_points:
                total_achieved_points += (
                    achieved_points / total_points * course_contest.points
                )
            total_contest_points += course_contest.points

        student_results["total"] = {
            "achieved_points": total_achieved_points,
            "total_points": total_contest_points,
            "percentage": (
                total_achieved_points / total_contest_points * 100
                if total_contest_points
                else 0
            ),
        }
        results[student] = student_results

    return results


def calculate_total_progress(lesson_progress, contest_progress):
    lesson_total = lesson_progress["total"]
    contest_total = contest_progress["total"]
    total_achieved_points = (
        lesson_total["achieved_points"] + contest_total["achieved_points"]
    )
    total_points = lesson_total["total_points"] + contest_total["total_points"]

    res = {
        "achieved_points": total_achieved_points,
        "total_points": total_points,
        "percentage": total_achieved_points / total_points * 100 if total_points else 0,
    }
    return res


class CoursePermissionMixin:
    """Mixin to handle course creation permissions and context"""

    def get_can_create_course(self):
        """Check if user can create courses"""
        if not self.request.user.is_authenticated:
            return False

        # Check if user is admin of any organization
        admin_org_ids = self.request.profile.get_admin_organization_ids()
        return bool(admin_org_ids)

    def get_course_context_data(self, context):
        """Add course-related context data"""
        context["can_create_course"] = self.get_can_create_course()
        return context


class CourseList(CoursePermissionMixin, DiggPaginatorMixin, ListView):
    model = Course
    template_name = "course/list.html"
    paginate_by = 10
    context_object_name = "courses"

    def get(self, request, *args, **kwargs):
        default_tab = "my" if request.user.is_authenticated else "joinable"
        self.current_tab = request.GET.get("tab", default_tab)
        self.search_query = request.GET.get("search", "")
        self.role_filter = request.GET.get("role_filter", "")
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        profile = self.request.profile if self.request.user.is_authenticated else None

        if self.current_tab == "my":
            if not profile:
                return Course.objects.none()
            queryset = Course.get_user_courses(profile)

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
            queryset = Course.get_joinable_courses(profile)

        if self.search_query:
            queryset = queryset.filter(
                Q(name__icontains=self.search_query)
                | Q(slug__icontains=self.search_query)
            )

        return queryset.order_by("-id").prefetch_related("organizations")

    def get_context_data(self, **kwargs):
        context = super(CourseList, self).get_context_data(**kwargs)
        context["title"] = _("Courses")
        context["page_type"] = (
            self.current_tab
        )  # Set page_type to current tab for active styling
        context["current_tab"] = self.current_tab
        context["search_query"] = self.search_query
        context["role_filter"] = self.role_filter

        # Build URL parameters for pagination
        url_params = []
        if self.current_tab:
            url_params.append(f"tab={self.current_tab}")
        if self.search_query:
            url_params.append(f"search={self.search_query}")
        if self.role_filter:
            url_params.append(f"role_filter={self.role_filter}")

        # Set pagination URLs that preserve tab and search parameters
        if url_params:
            param_string = "&".join(url_params)
            context["first_page_href"] = f"?{param_string}"
            context["page_prefix"] = f"?{param_string}&page="
            context["page_suffix"] = ""
        else:
            context["first_page_href"] = "."
            context["page_prefix"] = "?page="
            context["page_suffix"] = ""

        # Add course permission context using mixin
        context = self.get_course_context_data(context)

        return context


class CourseAdd(CoursePermissionMixin, LoginRequiredMixin, TitleMixin, CreateView):
    model = Course
    template_name = "course/create.html"
    form_class = AddCourseForm

    def get_title(self):
        return _("Add Course")

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            raise Http404()

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
    def dispatch(self, request, *args, **kwargs):
        self.course = get_object_or_404(Course, slug=self.kwargs["slug"])
        self.is_accessible = Course.is_accessible_by(self.course, self.request.profile)
        self.is_editable = Course.is_editable_by(self.course, self.request.profile)
        return super(CourseDetailMixin, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(CourseDetailMixin, self).get_context_data(**kwargs)
        context["course"] = self.course
        context["is_editable"] = self.is_editable
        context["is_accessible"] = self.is_accessible
        return context


class CourseEditableMixin(CourseDetailMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(CourseEditableMixin, self).dispatch(request, *args, **kwargs)
        if not self.is_editable:
            raise Http404()
        return res

    def get_user_role_in_course(self):
        """Get the current user's role in the course"""
        if self.request.user.is_superuser:
            return "ADMIN"
        try:
            course_role = CourseRole.objects.get(
                course=self.course, user=self.request.profile
            )
            return course_role.role
        except CourseRole.DoesNotExist:
            return None


class CourseAdminMixin(CourseDetailMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(CourseAdminMixin, self).dispatch(request, *args, **kwargs)
        # Allow admins, teachers, and assistants only
        if not (request.user.is_superuser or self.is_editable):
            raise Http404()

        # Double-check: ensure the user has an appropriate role
        current_role = self.get_user_role_in_course()
        if current_role not in ["ADMIN", RoleInCourse.TEACHER, RoleInCourse.ASSISTANT]:
            raise Http404()

        return res

    def get_user_role_in_course(self):
        """Get the current user's role in the course"""
        if self.request.user.is_superuser:
            return "ADMIN"
        try:
            course_role = CourseRole.objects.get(
                course=self.course, user=self.request.profile
            )
            return course_role.role
        except CourseRole.DoesNotExist:
            return None

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
    model = Course
    template_name = "course/course.html"

    def get_object(self):
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

            return context

        lessons = self.course.get_lessons()
        contests = self.course.get_contests()

        # Use bulk functions for single user (still more efficient due to better queries)
        students = [self.request.profile]
        all_problems = []
        for lesson in lessons:
            all_problems.extend(lesson.get_problems())

        bulk_problem_points = bulk_max_case_points_per_problem(students, all_problems)
        lesson_progress_bulk = bulk_calculate_lessons_progress(
            students, lessons, bulk_problem_points
        )
        contest_progress_bulk = bulk_calculate_contests_progress(students, contests)

        # Get user's role in the course
        user_role = None
        if self.request.user.is_authenticated:
            try:
                course_role = CourseRole.objects.get(
                    course=self.course, user=self.request.profile
                )
                user_role = course_role.role
            except CourseRole.DoesNotExist:
                user_role = None

        context["title"] = self.course.name
        context["page_type"] = "home"
        context["lessons"] = lessons
        context["lesson_progress"] = lesson_progress_bulk[self.request.profile]
        context["course_contests"] = contests
        context["contest_progress"] = contest_progress_bulk[self.request.profile]
        context["user_role"] = user_role

        context["total_progress"] = calculate_total_progress(
            context["lesson_progress"],
            context["contest_progress"],
        )

        return context


class CourseLessonDetail(CourseDetailMixin, DetailView):
    model = CourseLesson
    template_name = "course/lesson.html"

    def get_object(self):
        try:
            self.lesson = CourseLesson.objects.get(
                course=self.course, id=self.kwargs["id"]
            )

            if not Course.is_accessible_by(self.course, self.request.profile):
                raise Http404()

            return self.lesson
        except ObjectDoesNotExist:
            raise Http404()

    def get_profile(self):
        username = self.request.GET.get("user")
        if not username:
            return self.request.profile

        is_editable = Course.is_editable_by(self.course, self.request.profile)
        if not is_editable:
            raise Http404()

        try:
            profile = Profile.objects.get(user__username=username)
            is_student = profile.course_roles.filter(
                role=RoleInCourse.STUDENT, course=self.course
            ).exists()
            if not is_student:
                raise Http404()
            return profile
        except ObjectDoesNotExist:
            raise Http404()

    def get_context_data(self, **kwargs):
        context = super(CourseLessonDetail, self).get_context_data(**kwargs)
        profile = self.get_profile()
        problems = self.lesson.get_problems()

        # Use bulk function even for single user for consistency
        bulk_problem_points = bulk_max_case_points_per_problem([profile], problems)
        problem_points = bulk_problem_points.get(profile.id, {})

        context["profile"] = profile
        context["title"] = self.lesson.title
        context["lesson"] = self.lesson
        context["completed_problem_ids"] = user_completed_ids(profile)
        context["attempted_problems"] = user_attempted_ids(profile)
        context["problem_points"] = problem_points
        context["lesson_problems"] = [
            {"problem": p, "score": ps["score"]}
            for p, ps in zip(
                self.lesson.get_problems(), self.lesson.get_problems_and_scores()
            )
        ]

        # Get quizzes for this lesson
        lesson_quizzes = self.lesson.lesson_quizzes.filter(
            is_visible=True
        ).select_related("quiz")

        # Get quiz data with user's best scores and attempt info
        # Show best score from ALL attempts (not just lesson-linked)
        quiz_data = []
        for lesson_quiz in lesson_quizzes:
            quiz = lesson_quiz.quiz

            # Get best attempt from ALL quiz attempts (not just lesson-linked)
            best_attempt = (
                QuizAttempt.objects.filter(
                    quiz=quiz,
                    user=profile,
                    is_submitted=True,
                )
                .order_by("-score")
                .first()
            )

            # But count attempts only for lesson-linked attempts (for max_attempts enforcement)
            attempts_count = QuizAttempt.objects.filter(
                quiz=quiz,
                user=profile,
                lesson_quiz=lesson_quiz,
                is_submitted=True,
            ).count()

            # Check if user can make more attempts
            can_attempt = lesson_quiz.can_attempt(profile.user)

            quiz_data.append(
                {
                    "lesson_quiz": lesson_quiz,
                    "quiz": quiz,
                    "best_score": best_attempt.score if best_attempt else None,
                    "max_score": quiz.get_total_points(),
                    "attempts_count": attempts_count,
                    "max_attempts": lesson_quiz.max_attempts,
                    "can_attempt": can_attempt,
                    "points": lesson_quiz.points,
                }
            )

        context["lesson_quizzes"] = quiz_data
        return context


class CourseLessonForm(forms.ModelForm):
    class Meta:
        model = CourseLesson
        fields = ["order", "title", "is_visible", "points", "content"]
        widgets = {
            "title": forms.TextInput(),
            "content": HeavyPreviewPageDownWidget(preview=reverse_lazy("blog_preview")),
            "problems": HeavySelect2MultipleWidget(data_view="problem_select2"),
        }


CourseLessonFormSet = inlineformset_factory(
    Course, CourseLesson, form=CourseLessonForm, extra=1, can_delete=True
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


CourseLessonProblemFormSet = modelformset_factory(
    CourseLessonProblem, form=CourseLessonProblemForm, extra=0, can_delete=True
)


class CourseLessonQuizForm(ModelForm):
    class Meta:
        model = CourseLessonQuiz
        fields = ["order", "quiz", "points", "max_attempts", "is_visible", "lesson"]
        widgets = {
            "quiz": HeavySelect2Widget(
                data_view="quiz_select2", attrs={"style": "width: 100%"}
            ),
            "lesson": forms.HiddenInput(),
        }


CourseLessonQuizFormSet = modelformset_factory(
    CourseLessonQuiz, form=CourseLessonQuizForm, extra=0, can_delete=True
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
                "course_name": self.course.name,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "edit_lesson_new"
        context["lesson_field"] = CourseLessonForm()

        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form(form_class=CourseLessonForm)  # Get the CourseLessonForm

        if form.is_valid():
            with revisions.create_revision():
                form.instance.course_id = self.course.id
                self.lesson = form.save()
                formset = CourseLessonProblemFormSet(
                    data=self.request.POST,
                    prefix=f"problems_{self.lesson.id}" if self.lesson else "problems",
                    queryset=CourseLessonProblem.objects.filter(
                        lesson=self.lesson
                    ).order_by("order"),
                )
                for problem_formset in formset:
                    problem_formset.save()

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

    def dispatch(self, request, *args, **kwargs):
        # First, set up the course from CourseDetailMixin without calling FormView dispatch
        self.course = get_object_or_404(Course, slug=kwargs["slug"])
        if not Course.is_accessible_by(self.course, request.profile):
            raise Http404()
        self.is_editable = Course.is_editable_by(self.course, request.profile)

        # Check if user can edit (from CourseEditableMixin)
        if not self.is_editable:
            raise Http404()

        # Now fetch and validate the lesson belongs to this course
        try:
            lesson_id = kwargs.get("id")
            if not lesson_id:
                raise Http404("No lesson ID provided in URL")

            # Security: Ensure lesson belongs to the current course
            self.lesson = CourseLesson.objects.get(id=lesson_id, course=self.course)

        except CourseLesson.DoesNotExist:
            # More detailed error for debugging
            lesson_exists = CourseLesson.objects.filter(id=lesson_id).exists()
            if lesson_exists:
                actual_lesson = CourseLesson.objects.get(id=lesson_id)
                raise Http404(
                    f"Lesson {lesson_id} exists but belongs to course '{actual_lesson.course.slug}', not '{self.course.slug}'"
                )
            else:
                raise Http404(f"Lesson {lesson_id} does not exist")
        except Exception as e:
            raise Http404(f"Error accessing lesson: {e}")

        # Redirect if lesson doesn't have an ID (shouldn't happen but keeping original logic)
        if not self.lesson or not self.lesson.id:
            return HttpResponseRedirect(
                reverse(
                    "edit_course_lessons",
                    args=[self.course.slug],
                )
            )

        # Now that everything is set up, call the FormView dispatch
        return super(CourseEditableMixin, self).dispatch(request, *args, **kwargs)

    def get_user_role_in_course(self):
        """Get the current user's role in the course"""
        if self.request.user.is_superuser:
            return "ADMIN"
        try:
            course_role = CourseRole.objects.get(
                course=self.course, user=self.request.profile
            )
            return course_role.role
        except CourseRole.DoesNotExist:
            return None

    def get(self, request, *args, **kwargs):
        # Lesson is already validated and fetched in dispatch method
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
        for form in formset:
            form.fields["lesson"].initial = target_lesson
        # Also set the lesson for the empty_form (used as template for new rows)
        formset.empty_form.fields["lesson"].initial = target_lesson
        return formset

    def get_context_data(self, **kwargs):
        context = super(EditCourseLessonsViewNewWindow, self).get_context_data(**kwargs)

        if self.request.method != "POST":
            context["formset"] = self.form_class(
                instance=self.course, queryset=self.course.lessons.order_by("order")
            )
            # Create problem formset only for the current lesson
            context["problem_formsets"] = {
                self.lesson.id: self.get_problem_formset(post=False, lesson=self.lesson)
            }
            # Create quiz formset for the current lesson
            context["quiz_formset"] = self.get_quiz_formset(
                post=False, lesson=self.lesson
            )

        context["title"] = _("Edit lessons for %(course_name)s") % {
            "course_name": self.course.name
        }
        context["content_title"] = mark_safe(
            _("Edit lessons for <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": self.course.name,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "edit_lesson_new"
        context["lesson_field"] = CourseLessonForm(instance=self.lesson)
        context["lesson"] = self.lesson
        context["current_user_role"] = self.get_user_role_in_course()

        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form(form_class=CourseLessonForm)  # Get the CourseLessonForm
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
                    self.lesson = form.save()

                    # Add revision tracking details for lesson editing
                    revisions.set_comment(
                        _("Updated lesson '{}' in course {}").format(
                            form.cleaned_data["title"], self.course.name
                        )
                    )
                    revisions.set_user(request.user)

                    problem_formsets = self.get_problem_formset(
                        post=True, lesson=self.lesson
                    )
                    if problem_formsets.is_valid():
                        problem_formsets.save()
                        for obj in problem_formsets.deleted_objects:
                            if obj.pk is not None:
                                obj.delete()

                    quiz_formsets = self.get_quiz_formset(post=True, lesson=self.lesson)
                    if quiz_formsets.is_valid():
                        quiz_formsets.save()
                        for obj in quiz_formsets.deleted_objects:
                            if obj.pk is not None:
                                obj.delete()
            return self.form_valid(form)
        else:
            return self.form_invalid(form)

    def form_valid(self, form):
        if "delete_lesson" in self.request.POST:
            return redirect("edit_course_lessons", slug=self.course.slug)
        else:
            return super().form_valid(form)

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def get_success_url(self):
        return reverse(
            "edit_course_lessons",
            args=[self.course.slug],
        )


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
                "course_name": self.course.name,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "edit_lesson"

        return context

    def post(self, request, *args, **kwargs):
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
                problem_formset.save()
                for obj in problem_formset.deleted_objects:
                    if obj.pk is not None:
                        obj.delete()
            return self.form_valid(formset)
        else:
            return self.form_invalid(formset)

    def get_success_url(self):
        return self.request.path


class CourseStudentResults(CourseEditableMixin, DetailView):
    model = Course
    template_name = "course/grades.html"

    def get_object(self):
        self.lessons = self.course.get_lessons()
        self.contests = self.course.get_contests()
        return self.course

    def get_grades(self):
        students = self.course.get_students()

        # Collect all problems from all lessons for bulk query
        all_problems = []
        for lesson in self.lessons:
            all_problems.extend(lesson.get_problems())

        bulk_problem_points = bulk_max_case_points_per_problem(students, all_problems)
        grade_lessons = bulk_calculate_lessons_progress(
            students, self.lessons, bulk_problem_points
        )
        grade_contests = bulk_calculate_contests_progress(students, self.contests)

        grade_total = {}
        for student in students:
            grade_total[student] = calculate_total_progress(
                grade_lessons[student], grade_contests[student]
            )

        students.sort(key=lambda s: (-grade_total[s]["percentage"], s.username.lower()))

        grade_lessons = {s: grade_lessons[s] for s in students}
        grade_contests = {s: grade_contests[s] for s in students}
        grade_total = {s: grade_total[s] for s in students}

        return grade_lessons, grade_contests, grade_total

    def get_context_data(self, **kwargs):
        context = super(CourseStudentResults, self).get_context_data(**kwargs)
        context["title"] = _("Grades in %(course_name)s") % {
            "course_name": self.course.name,
        }
        context["content_title"] = mark_safe(
            _("Grades in <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": self.course.name,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "grades"
        (
            context["grade_lessons"],
            context["grade_contests"],
            context["grade_total"],
        ) = self.get_grades()
        context["lessons"] = self.lessons
        context["course_contests"] = self.contests
        return context


class CourseStudentResultsLesson(CourseEditableMixin, DetailView):
    model = CourseLesson
    template_name = "course/grades_lesson.html"

    def get_object(self):
        try:
            self.lesson = CourseLesson.objects.get(
                course=self.course, id=self.kwargs["id"]
            )
            # Security: Only allow access to visible lessons or if user can edit course
            if not self.lesson.is_visible and not Course.is_editable_by(
                self.course, self.request.profile
            ):
                raise Http404()

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
        students = self.course.get_students()

        bulk_problem_points = bulk_max_case_points_per_problem(students, self.problems)

        # Bulk fetch best quiz scores for all students and lesson quizzes
        student_ids = [s.id for s in students]
        quiz_ids = [lq.quiz_id for lq in self.lesson_quizzes]

        # Pre-compute current quiz totals (use quiz.get_total_points() for current value)
        quiz_totals = {
            lq.quiz_id: lq.quiz.get_total_points() for lq in self.lesson_quizzes
        }

        # Get best score for each (user, quiz) combination
        best_quiz_scores = {}
        if quiz_ids and student_ids:
            from django.db.models import Max

            best_attempts = (
                QuizAttempt.objects.filter(
                    user_id__in=student_ids,
                    quiz_id__in=quiz_ids,
                    is_submitted=True,
                )
                .values("user_id", "quiz_id")
                .annotate(
                    best_score=Max("score"),
                )
            )

            for attempt in best_attempts:
                best_quiz_scores[(attempt["user_id"], attempt["quiz_id"])] = {
                    "score": float(attempt["best_score"] or 0),
                }

        grades = {}
        for student in students:
            student_points = bulk_problem_points.get(student.id, {})
            grades[student] = dict(student_points)

            achieved_points = total_points = 0

            # Calculate problem points
            for ps in self.lesson.get_problems_and_scores():
                problem_data = student_points.get(ps["problem_id"])
                if problem_data and problem_data["case_total"]:
                    achieved_points += (
                        problem_data["case_points"]
                        / problem_data["case_total"]
                        * ps["score"]
                    )
                total_points += ps["score"]

            # Calculate quiz points
            for lesson_quiz in self.lesson_quizzes:
                quiz_points = lesson_quiz.points or 0
                total_points += quiz_points

                # Use current quiz total, not the one stored in attempts
                quiz_max_score = quiz_totals.get(lesson_quiz.quiz_id, 0)
                quiz_data = best_quiz_scores.get((student.id, lesson_quiz.quiz_id))

                if quiz_data and quiz_max_score > 0:
                    score_ratio = quiz_data["score"] / quiz_max_score
                    achieved_points += score_ratio * quiz_points
                    grades[student][f"quiz_{lesson_quiz.id}"] = {
                        "score": quiz_data["score"],
                        "max_score": quiz_max_score,
                        "achieved": score_ratio * quiz_points,
                    }
                else:
                    grades[student][f"quiz_{lesson_quiz.id}"] = {
                        "score": 0,
                        "max_score": quiz_max_score,
                        "achieved": 0,
                    }

            grades[student]["total"] = {
                "achieved_points": achieved_points,
                "total_points": total_points,
                "percentage": (
                    achieved_points / total_points * 100 if total_points else 0
                ),
            }

        # Sort students by total percentage (descending), then by username
        students.sort(
            key=lambda s: (-grades[s]["total"]["percentage"], s.username.lower())
        )

        # Return grades in sorted order
        sorted_grades = {}
        for student in students:
            sorted_grades[student] = grades[student]

        return sorted_grades

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
                "course_name": self.course.name,
                "lesson_name": self.lesson.title,
                "url_course": self.course.get_absolute_url(),
                "url_lesson": self.lesson.get_absolute_url(),
            }
        )
        context["page_type"] = "grades"
        context["grades"] = self.get_lesson_grades()
        context["problems"] = [
            {"problem": p, "score": ps["score"]}
            for p, ps in zip(self.problems, self.lesson.get_problems_and_scores())
        ]
        context["lesson_quizzes"] = self.lesson_quizzes
        return context


class AddCourseContestForm(forms.ModelForm):
    order = forms.IntegerField(label=_("Order"))
    points = forms.IntegerField(label=_("Points"))

    class Meta:
        model = Contest
        fields = [
            "order",
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

        CourseContest.objects.create(
            course=course,
            contest=contest,
            order=self.cleaned_data["order"],
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
        return reverse(
            "edit_course_contest",
            args=[self.course.slug, self.contest.key],
        )


class CourseContestList(CourseEditableMixin, ListView):
    template_name = "course/contest_list.html"
    context_object_name = "course_contests"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Contest list")
        context["content_title"] = mark_safe(
            _("Edit contests for <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": self.course.name,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "contests"
        return context

    def get_queryset(self):
        return self.course.contests.select_related("contest").all().order_by("order")


class EditCourseContestForm(ModelForm):
    order = forms.IntegerField(label=_("Order"))
    points = forms.IntegerField(label=_("Points"))

    class Meta:
        model = Contest
        fields = (
            "order",
            "points",
            "is_visible",
            "key",
            "name",
            "start_time",
            "end_time",
            "format_name",
            "authors",
            "curators",
            "testers",
            "time_limit",
            "freeze_after",
            "use_clarifications",
            "hide_problem_tags",
            "public_scoreboard",
            "scoreboard_visibility",
            "points_precision",
            "rate_limit",
            "description",
            "access_code",
            "private_contestants",
            "view_contest_scoreboard",
            "banned_users",
        )
        widgets = {
            "authors": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "curators": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "testers": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "private_contestants": HeavySelect2MultipleWidget(
                data_view="profile_select2"
            ),
            "banned_users": HeavySelect2MultipleWidget(data_view="profile_select2"),
            "view_contest_scoreboard": HeavySelect2MultipleWidget(
                data_view="profile_select2"
            ),
            "tags": Select2MultipleWidget,
            "description": HeavyPreviewPageDownWidget(
                preview=reverse_lazy("contest_preview")
            ),
            "start_time": DateTimePickerWidget(),
            "end_time": DateTimePickerWidget(),
            "format_name": Select2Widget(),
            "scoreboard_visibility": Select2Widget(),
        }

    def __init__(self, *args, **kwargs):
        self.course_contest_instance = kwargs.pop("course_contest_instance", None)
        super().__init__(*args, **kwargs)

        if self.course_contest_instance:
            self.fields["order"].initial = self.course_contest_instance.order
            self.fields["points"].initial = self.course_contest_instance.points

    def save(self, commit=True):
        contest = super().save(commit=commit)

        if self.course_contest_instance:
            self.course_contest_instance.order = self.cleaned_data["order"]
            self.course_contest_instance.points = self.cleaned_data["points"]
            if commit:
                self.course_contest_instance.save()

        return contest


class EditCourseContest(CourseEditableMixin, FormView):
    template_name = "course/edit_contest.html"
    form_class = EditCourseContestForm

    def dispatch(self, request, *args, **kwargs):
        self.contest = get_object_or_404(Contest, key=self.kwargs["contest"])
        res = super().dispatch(request, *args, **kwargs)
        if not self.contest.is_in_course:
            raise Http404()
        return res

    def get_form_kwargs(self):
        self.course_contest = get_object_or_404(
            CourseContest, course=self.course, contest=self.contest
        )

        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.contest
        kwargs["course_contest_instance"] = self.course_contest
        return kwargs

    def post(self, request, *args, **kwargs):
        problem_formset = self.get_problem_formset(True)
        quiz_formset = self.get_quiz_formset(True)

        problems_valid = problem_formset.is_valid()
        quizzes_valid = quiz_formset.is_valid()

        if problems_valid and quizzes_valid:
            self.problem_form_changes = False

            # Process problem formset
            for problem_form in problem_formset:
                if problem_form.has_changed():
                    self.problem_form_changes = True
                if problem_form.cleaned_data.get("DELETE") and problem_form.instance.pk:
                    problem_form.instance.delete()

            for problem_form in problem_formset.save(commit=False):
                if problem_form:
                    problem_form.contest = self.contest
                    problem_form.save()

            # Process quiz formset
            for quiz_form in quiz_formset:
                if quiz_form.has_changed():
                    self.problem_form_changes = True
                if quiz_form.cleaned_data.get("DELETE") and quiz_form.instance.pk:
                    quiz_form.instance.delete()

            for quiz_form in quiz_formset.save(commit=False):
                if quiz_form:
                    quiz_form.contest = self.contest
                    quiz_form.save()

            return super().post(request, *args, **kwargs)

        self.object = self.contest
        return self.render_to_response(
            self.get_context_data(
                problems_form=problem_formset,
                quizzes_form=quiz_formset,
            )
        )

    def get_title(self):
        return _("Edit contest")

    def form_valid(self, form):
        with revisions.create_revision():
            revisions.set_comment(_("Edited from course") + " " + self.course.name)
            revisions.set_user(self.request.user)

            if self.problem_form_changes:
                maybe_trigger_contest_rescore(form, self.contest, True)

            form.save()

        return super().form_valid(form)

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
        context["title"] = self.get_title()
        context["content_title"] = mark_safe(
            _("Edit <a href='%(url)s'>%(contest_name)s</a>")
            % {
                "contest_name": self.contest.name,
                "url": self.contest.get_absolute_url(),
            }
        )
        if "problems_form" not in context:
            context["problems_form"] = self.get_problem_formset()
        if "quizzes_form" not in context:
            context["quizzes_form"] = self.get_quiz_formset()
        return context

    def get_success_url(self):
        return reverse(
            "edit_course_contest",
            args=[self.course.slug, self.contest.key],
        )


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
        from django.http import JsonResponse
        from django.utils.encoding import smart_str

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
    user = forms.CharField(
        max_length=150,
        widget=HeavySelect2Widget(
            data_view="profile_select2", attrs={"style": "width: 100%"}
        ),
        label=_("User"),
    )
    role = forms.ChoiceField(
        choices=RoleInCourse.choices, widget=Select2Widget(), label=_("Role")
    )

    def __init__(self, *args, course=None, current_user_role=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.course = course
        self.current_user_role = current_user_role

        # Restrict role choices based on current user's role
        if current_user_role == RoleInCourse.ASSISTANT:
            # Assistants can only assign assistant and student roles
            self.fields["role"].choices = [
                (RoleInCourse.ASSISTANT, _("Assistant")),
                (RoleInCourse.STUDENT, _("Student")),
            ]
        # Teachers and Admins get all choices by default

    def clean_user(self):
        user_id = self.cleaned_data["user"]
        try:
            # The HeavySelect2Widget returns the profile ID, not username
            profile = Profile.objects.get(id=user_id)
            return profile
        except (Profile.DoesNotExist, ValueError):
            raise ValidationError(_("User does not exist."))

    def clean(self):
        cleaned_data = super().clean()
        profile = cleaned_data.get("user")
        role = cleaned_data.get("role")

        if profile and self.course:
            # Check if user is already in the course
            if CourseRole.objects.filter(course=self.course, user=profile).exists():
                raise ValidationError(_("User is already a member of this course."))

        # Validate role assignment permissions
        if role and self.current_user_role:
            if self.current_user_role == RoleInCourse.ASSISTANT and role not in [
                RoleInCourse.ASSISTANT,
                RoleInCourse.STUDENT,
            ]:
                raise ValidationError(
                    _("Assistants can only assign assistant and student roles.")
                )

        return cleaned_data


class CourseMembers(CourseAdminMixin, FormView):
    template_name = "course/members.html"
    form_class = CourseMemberForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["course"] = self.course
        kwargs["current_user_role"] = self.get_user_role_in_course()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super(CourseMembers, self).get_context_data(**kwargs)

        # Get all course members with their roles, ordered by role priority (Teacher, Assistant, Student)
        from django.db.models import Case, When, Value, IntegerField

        members = (
            CourseRole.objects.filter(course=self.course)
            .select_related("user")
            .annotate(
                role_priority=Case(
                    When(role=RoleInCourse.TEACHER, then=Value(1)),
                    When(role=RoleInCourse.ASSISTANT, then=Value(2)),
                    When(role=RoleInCourse.STUDENT, then=Value(3)),
                    default=Value(4),
                    output_field=IntegerField(),
                )
            )
            .order_by("role_priority", "user__user__username")
        )

        context["members"] = members
        context["title"] = _("Manage members for %(course_name)s") % {
            "course_name": self.course.name
        }
        context["content_title"] = mark_safe(
            _("Manage members for <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": self.course.name,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "members"
        context["role_choices"] = RoleInCourse.choices
        context["current_user_role"] = self.get_user_role_in_course()

        return context

    def form_valid(self, form):
        with revisions.create_revision():
            profile = form.cleaned_data[
                "user"
            ]  # This is now a Profile object from clean_user
            role = form.cleaned_data["role"]

            # Create the course role (form validation ensures user is not already in course)
            course_role = CourseRole.objects.create(
                course=self.course, user=profile, role=role
            )

            # Add revision tracking details
            revisions.set_comment(
                _("Added member '{}' with role {} to course {}").format(
                    profile.user.username,
                    course_role.get_role_display(),
                    self.course.name,
                )
            )
            revisions.set_user(self.request.user)

            messages.success(self.request, _("User added successfully."))
            return super().form_valid(form)

    def get_success_url(self):
        return reverse("course_members", args=[self.course.slug])


class CourseRemoveMember(CourseAdminMixin, View):
    def post(self, request, *args, **kwargs):
        member_id = request.POST.get("member_id")

        try:
            course_role = CourseRole.objects.get(id=member_id, course=self.course)

            current_user_role = self.get_user_role_in_course()
            target_role = course_role.role

            # Check permissions for member removal
            if not self.user_can_manage_user(target_role):
                messages.error(
                    request, _("You do not have permission to remove this user.")
                )
                return HttpResponseRedirect(
                    reverse("course_members", args=[self.course.slug])
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
        except CourseRole.DoesNotExist:
            messages.error(request, _("Member not found."))

        return HttpResponseRedirect(reverse("course_members", args=[self.course.slug]))


class CourseUpdateMemberRole(CourseAdminMixin, View):
    def post(self, request, *args, **kwargs):
        member_id = request.POST.get("member_id")
        new_role = request.POST.get("role")

        if new_role not in [choice[0] for choice in RoleInCourse.choices]:
            messages.error(request, _("Invalid role selected."))
            return HttpResponseRedirect(
                reverse("course_members", args=[self.course.slug])
            )

        try:
            course_role = CourseRole.objects.get(id=member_id, course=self.course)

            current_user_role = self.get_user_role_in_course()
            old_role = course_role.role

            # Check permissions for role changes
            # First check if user can manage the target user
            if not self.user_can_manage_user(old_role):
                messages.error(
                    request, _("You do not have permission to manage this user.")
                )
                return HttpResponseRedirect(
                    reverse("course_members", args=[self.course.slug])
                )

            # Then check if user can assign the new role
            if not self.user_can_assign_role(new_role):
                messages.error(
                    request, _("You do not have permission to assign this role.")
                )
                return HttpResponseRedirect(
                    reverse("course_members", args=[self.course.slug])
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
        except CourseRole.DoesNotExist:
            messages.error(request, _("Member not found."))

        return HttpResponseRedirect(reverse("course_members", args=[self.course.slug]))


class CourseEditForm(forms.ModelForm):
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
            "course_image": ImageWidget,
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

        # Check if user is superuser to determine field visibility
        is_superuser = self.request and self.request.user.is_superuser
        is_teacher = self.user_role == RoleInCourse.TEACHER or is_superuser
        is_assistant = self.user_role == RoleInCourse.ASSISTANT

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
                from django.utils.html import format_html
                from django.urls import reverse

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
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = _("Edit %(course_name)s") % {"course_name": self.course.name}
        context["content_title"] = mark_safe(
            _('Edit <a href="%(url)s">%(course_name)s</a>')
            % {
                "course_name": self.course.name,
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

    def post(self, request, slug):
        course = get_object_or_404(Course, slug=slug)
        profile = request.profile

        # Check if user can join this course
        if not Course.is_joinable(course, profile):
            messages.error(request, _("You cannot join this course."))
            return redirect("course_list")

        # Check if user is already enrolled
        if CourseRole.objects.filter(course=course, user=profile).exists():
            messages.warning(request, _("You are already enrolled in this course."))
            return redirect("course_detail", slug=course.slug)

        # Join the course as a student
        with revisions.create_revision():
            CourseRole.objects.create(
                course=course, user=profile, role=RoleInCourse.STUDENT
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

    def post(self, request, slug):
        course = get_object_or_404(Course, slug=slug)
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
