from copy import deepcopy
from django import forms
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import Max, F, Sum
from django.forms import (
    inlineformset_factory,
    ModelForm,
    modelformset_factory,
)
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy, reverse
from django.utils.html import mark_safe
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, DetailView, View
from django.views.generic.edit import FormView
from reversion import revisions

from judge.forms import (
    ContestProblemFormSet,
    LessonCloneForm,
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
from judge.utils.views import SingleObjectFormView, TitleMixin


def max_case_points_per_problem(profile, problems):
    # return a dict {problem_id: {case_points, case_total}}
    q = (
        Submission.objects.filter(user=profile, problem__in=problems)
        .values("problem")
        .annotate(case_points=Max("case_points"), case_total=F("case_total"))
        .order_by("problem")
    )
    res = {}
    for problem in q:
        res[problem["problem"]] = problem
    return res


def calculate_lessons_progress(profile, lessons):
    res = {}
    total_achieved_points = total_lesson_points = 0
    for lesson in lessons:
        problems = lesson.lesson_problems.values_list("problem", flat=True)
        problem_points = max_case_points_per_problem(profile, problems)
        achieved_points = total_points = 0

        for lesson_problem in lesson.lesson_problems.all():
            val = problem_points.get(lesson_problem.problem.id)
            if val and val["case_total"]:
                achieved_points += (
                    val["case_points"] / val["case_total"] * lesson_problem.score
                )
            total_points += lesson_problem.score

        res[lesson.id] = {
            "achieved_points": achieved_points,
            "total_points": total_points,
            "percentage": achieved_points / total_points * 100 if total_points else 0,
        }
        if total_points:
            total_achieved_points += achieved_points / total_points * lesson.points
        total_lesson_points += lesson.points

    res["total"] = {
        "achieved_points": total_achieved_points,
        "total_points": total_lesson_points,
        "percentage": (
            total_achieved_points / total_lesson_points * 100
            if total_lesson_points
            else 0
        ),
    }
    return res


def calculate_contests_progress(profile, course_contests):
    res = {}
    total_achieved_points = total_contest_points = 0
    for course_contest in course_contests:
        contest = course_contest.contest

        achieved_points = 0
        participation = ContestParticipation.objects.filter(
            contest=contest, user=profile, virtual=0
        ).first()

        if participation:
            achieved_points = participation.score

        total_points = (
            ContestProblem.objects.filter(contest=contest).aggregate(Sum("points"))[
                "points__sum"
            ]
            or 0
        )

        res[course_contest.id] = {
            "achieved_points": achieved_points,
            "total_points": total_points,
            "percentage": achieved_points / total_points * 100 if total_points else 0,
        }

        if total_points:
            total_achieved_points += (
                achieved_points / total_points * course_contest.points
            )
        total_contest_points += course_contest.points

    res["total"] = {
        "achieved_points": total_achieved_points,
        "total_points": total_contest_points,
        "percentage": (
            total_achieved_points / total_contest_points * 100
            if total_contest_points
            else 0
        ),
    }
    return res


def calculate_total_progress(profile, lesson_progress, contest_progress):
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


class CourseList(ListView):
    model = Course
    template_name = "course/list.html"
    queryset = Course.objects.filter(is_public=True).filter(is_open=True)

    def get_context_data(self, **kwargs):
        context = super(CourseList, self).get_context_data(**kwargs)
        context["courses"] = Course.get_accessible_courses(self.request.profile)
        context["title"] = _("Courses")
        context["page_type"] = "list"
        return context


class CourseDetailMixin(object):
    def dispatch(self, request, *args, **kwargs):
        self.course = get_object_or_404(Course, slug=self.kwargs["slug"])
        if not Course.is_accessible_by(self.course, self.request.profile):
            raise Http404()
        self.is_editable = Course.is_editable_by(self.course, self.request.profile)
        return super(CourseDetailMixin, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(CourseDetailMixin, self).get_context_data(**kwargs)
        context["course"] = self.course
        context["is_editable"] = self.is_editable
        return context


class CourseEditableMixin(CourseDetailMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(CourseEditableMixin, self).dispatch(request, *args, **kwargs)
        if not self.is_editable:
            raise Http404()
        return res


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

    def get_context_data(self, **kwargs):
        context = super(CourseDetail, self).get_context_data(**kwargs)
        lessons = (
            self.course.lessons.filter(is_visible=True)
            .order_by("order")
            .prefetch_related("lesson_problems")
            .all()
        )
        course_contests = (
            self.course.contests.select_related("contest")
            .filter(contest__is_visible=True)
            .order_by("order")
        )
        context["title"] = self.course.name
        context["page_type"] = "home"
        context["lessons"] = lessons
        context["lesson_progress"] = calculate_lessons_progress(
            self.request.profile, lessons
        )
        context["course_contests"] = course_contests
        context["contest_progress"] = calculate_contests_progress(
            self.request.profile, course_contests
        )

        context["total_progress"] = calculate_total_progress(
            self.request.profile,
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

            is_editable = Course.is_editable_by(self.course, self.request.profile)
            if not self.lesson.is_visible and not is_editable:
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
        context["profile"] = profile
        context["title"] = self.lesson.title
        context["lesson"] = self.lesson
        context["completed_problem_ids"] = user_completed_ids(profile)
        context["attempted_problems"] = user_attempted_ids(profile)
        context["problem_points"] = max_case_points_per_problem(
            profile, self.lesson.lesson_problems.values_list("problem", flat=True)
        )
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
    CourseLessonProblem, form=CourseLessonProblemForm, extra=5, can_delete=True
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
        return self.course

    def get_grades(self):
        students = self.course.get_students()

        lessons = (
            self.course.lessons.filter(is_visible=True)
            .prefetch_related("lesson_problems")
            .all()
        )
        course_contests = (
            self.course.contests.select_related("contest")
            .filter(contest__is_visible=True)
            .order_by("order")
        )

        grade_lessons = {}
        grade_contests = {}
        grade_total = {}
        for s in students:
            grade_lessons[s] = lesson_progress = calculate_lessons_progress(s, lessons)
            grade_contests[s] = contest_progress = calculate_contests_progress(
                s, course_contests
            )
            grade_total[s] = calculate_total_progress(
                s, lesson_progress, contest_progress
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
        context["lessons"] = self.course.lessons.filter(is_visible=True).order_by(
            "order"
        )
        context["course_contests"] = (
            self.course.contests.select_related("contest")
            .filter(contest__is_visible=True)
            .order_by("order")
        )
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
            return self.lesson
        except ObjectDoesNotExist:
            raise Http404()

    def get_lesson_grades(self):
        students = self.course.get_students()
        students.sort(key=lambda u: u.username.lower())
        problems = self.lesson.lesson_problems.values_list("problem", flat=True)
        lesson_problems = self.lesson.lesson_problems.all()
        grades = {}
        for s in students:
            grades[s] = problem_points = max_case_points_per_problem(s, problems)
            achieved_points = total_points = 0
            for lesson_problem in lesson_problems:
                val = problem_points.get(lesson_problem.problem.id)
                if val and val["case_total"]:
                    achieved_points += (
                        val["case_points"] / val["case_total"] * lesson_problem.score
                    )
                total_points += lesson_problem.score
            grades[s]["total"] = {
                "achieved_points": achieved_points,
                "total_points": total_points,
                "percentage": (
                    achieved_points / total_points * 100 if total_points else 0
                ),
            }
        return grades

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
        if problem_formset.is_valid():
            self.problem_form_changes = False
            for problem_form in problem_formset:
                if problem_form.has_changed():
                    self.problem_form_changes = True
                if problem_form.cleaned_data.get("DELETE") and problem_form.instance.pk:
                    problem_form.instance.delete()

            for problem_form in problem_formset.save(commit=False):
                if problem_form:
                    problem_form.contest = self.contest
                    problem_form.save()

            return super().post(request, *args, **kwargs)

        self.object = self.contest
        return self.render_to_response(
            self.get_context_data(
                problems_form=problem_formset,
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
            queryset=ContestProblem.objects.filter(contest=self.contest).order_by(
                "order"
            ),
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
    class Meta:
        model = Course
        fields = ["name", "about", "is_public", "slug", "course_image"]
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
            "course_image": _(
                "Optional. Upload an image for the course (maximum 5MB)."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make required fields as specified by user
        self.fields["name"].required = True
        self.fields["about"].required = False  # Description is optional
        self.fields["slug"].required = True
        self.fields["course_image"].required = False  # Image is optional

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
        if course_image:
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
