from django.utils.html import mark_safe
from django.db import models
from django.views.generic import ListView, DetailView, View
from django.utils.translation import gettext, gettext_lazy as _
from django.http import Http404
from django import forms
from django.forms import (
    inlineformset_factory,
    ModelForm,
    modelformset_factory,
    BaseModelFormSet,
)
from django.views.generic.edit import FormView
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.db.models import Max, F
from django.core.exceptions import ObjectDoesNotExist

from judge.models import (
    Course,
    CourseLesson,
    Submission,
    Profile,
    CourseRole,
    CourseLessonProblem,
)
from judge.models.course import RoleInCourse
from judge.widgets import (
    HeavyPreviewPageDownWidget,
    HeavySelect2MultipleWidget,
    HeavySelect2Widget,
)
from judge.utils.problems import (
    user_attempted_ids,
    user_completed_ids,
)


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
        "percentage": total_achieved_points / total_lesson_points * 100
        if total_lesson_points
        else 0,
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


class CourseDetail(CourseDetailMixin, DetailView):
    model = Course
    template_name = "course/course.html"

    def get_object(self):
        return self.course

    def get_context_data(self, **kwargs):
        context = super(CourseDetail, self).get_context_data(**kwargs)
        lessons = self.course.lessons.prefetch_related("problems").all()
        context["title"] = self.course.name
        context["page_type"] = "home"
        context["lessons"] = lessons
        context["lesson_progress"] = calculate_lessons_progress(
            self.request.profile, lessons
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
        fields = ["order", "title", "points", "content", "problems"]
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
        if self.request.method == "POST":
            context["formset"] = self.form_class(
                self.request.POST, self.request.FILES, instance=self.course
            )
            context["problem_formsets"] = {
                lesson.instance.id: self.get_problem_formset(
                    post=True, lesson=lesson.instance
                )
                for lesson in context["formset"].forms
                if lesson.instance.id
            }
        else:
            context["formset"] = self.form_class(
                instance=self.course, queryset=self.course.lessons.order_by("order")
            )
            context["problem_formsets"] = {
                lesson.instance.id: self.get_problem_formset(
                    post=False, lesson=lesson.instance
                )
                for lesson in context["formset"].forms
                if lesson.instance.id
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
        students.sort(key=lambda u: u.username.lower())
        lessons = self.course.lessons.prefetch_related("problems").all()
        grades = {s: calculate_lessons_progress(s, lessons) for s in students}
        return grades

    def get_context_data(self, **kwargs):
        context = super(CourseStudentResults, self).get_context_data(**kwargs)
        context["title"] = _("Grades in %(course_name)s</a>") % {
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
        context["grades"] = self.get_grades()
        return context


class CourseStudentResultsLesson(CourseEditableMixin, DetailView):
    model = CourseLesson
    template_name = "course/grades_lesson.html"

    def get_object(self):
        try:
            self.lesson = CourseLesson.objects.get(
                course=self.course, id=self.kwargs["id"]
            )
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
                "percentage": achieved_points / total_points * 100
                if total_points
                else 0,
            }
        return grades

    def get_context_data(self, **kwargs):
        context = super(CourseStudentResultsLesson, self).get_context_data(**kwargs)
        context["lesson"] = self.lesson
        context["title"] = _("Grades of %(lesson_name)s</a> in %(course_name)s</a>") % {
            "course_name": self.course.name,
            "lesson_name": self.lesson.title,
        }
        context["content_title"] = mark_safe(
            _("Grades of %(lesson_name)s</a> in <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": self.course.name,
                "lesson_name": self.lesson.title,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "grades"
        context["grades"] = self.get_lesson_grades()
        return context
