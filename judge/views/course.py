from django.utils.html import mark_safe
from django.db import models
from django.views.generic import ListView, DetailView, View
from django.utils.translation import gettext, gettext_lazy as _
from django.http import Http404
from django import forms
from django.forms import inlineformset_factory
from django.views.generic.edit import FormView
from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.db.models import Max, F
from django.core.exceptions import ObjectDoesNotExist

from judge.models import Course, CourseLesson, Submission, Profile, CourseRole
from judge.models.course import RoleInCourse
from judge.widgets import HeavyPreviewPageDownWidget, HeavySelect2MultipleWidget
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
    total_achieved_points = 0
    total_points = 0
    for lesson in lessons:
        problems = list(lesson.problems.all())
        if not problems:
            res[lesson.id] = {"achieved_points": 0, "percentage": 0}
            total_points += lesson.points
            continue
        problem_points = max_case_points_per_problem(profile, problems)
        num_problems = len(problems)
        percentage = 0
        for val in problem_points.values():
            score = val["case_points"] / val["case_total"]
            percentage += score / num_problems
        res[lesson.id] = {
            "achieved_points": percentage * lesson.points,
            "percentage": percentage * 100,
        }
        total_achieved_points += percentage * lesson.points
        total_points += lesson.points

    res["total"] = {
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
        context["title"] = self.lesson.title
        context["lesson"] = self.lesson
        context["completed_problem_ids"] = user_completed_ids(profile)
        context["attempted_problems"] = user_attempted_ids(profile)
        context["problem_points"] = max_case_points_per_problem(
            profile, self.lesson.problems.all()
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


class EditCourseLessonsView(CourseEditableMixin, FormView):
    template_name = "course/edit_lesson.html"
    form_class = CourseLessonFormSet

    def get_context_data(self, **kwargs):
        context = super(EditCourseLessonsView, self).get_context_data(**kwargs)
        if self.request.method == "POST":
            context["formset"] = self.form_class(
                self.request.POST, self.request.FILES, instance=self.course
            )
        else:
            context["formset"] = self.form_class(
                instance=self.course, queryset=self.course.lessons.order_by("order")
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
        context["page_type"] = "edit_lesson"

        return context

    def post(self, request, *args, **kwargs):
        formset = self.form_class(request.POST, instance=self.course)
        if formset.is_valid():
            formset.save()
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
        context["title"] = mark_safe(
            _("Grades in <a href='%(url)s'>%(course_name)s</a>")
            % {
                "course_name": self.course.name,
                "url": self.course.get_absolute_url(),
            }
        )
        context["page_type"] = "grades"
        context["grades"] = self.get_grades()
        return context
