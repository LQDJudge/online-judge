from django.contrib import messages
from django.db.transaction import commit
from django.utils.html import mark_safe
from django.db import models
from django.views.generic import ListView, DetailView, View
from django.utils.translation import gettext, gettext_lazy as _
from django.http import Http404, HttpResponseRedirect
from django import forms
from django.forms import (
    inlineformset_factory,
    ModelForm,
    modelformset_factory,
    BaseModelFormSet,
)
from django.views.generic.edit import FormView
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse_lazy, reverse
from django.db.models import Max, F, Sum
from django.core.exceptions import ObjectDoesNotExist

from judge.models import (
    Course,
    Contest,
    CourseLesson,
    Submission,
    Profile,
    CourseRole,
    CourseLessonProblem,
    CourseContest,
    ContestProblem,
    ContestParticipation,
)
from judge.models.course import RoleInCourse
from judge.widgets import (
    HeavyPreviewPageDownWidget,
    HeavySelect2MultipleWidget,
    HeavySelect2Widget,
    DateTimePickerWidget,
    Select2MultipleWidget,
    Select2Widget,
)
from judge.forms import (
    ContestProblemFormSet,
)
from judge.utils.problems import (
    user_attempted_ids,
    user_completed_ids,
)
from judge.utils.contest import (
    maybe_trigger_contest_rescore,
)
from reversion import revisions


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
        "percentage": total_achieved_points / total_contest_points * 100
        if total_contest_points
        else 0,
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
        # get = super().get(request, *args, **kwargs)

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
        # context["lesson"] = self.lesson

        return context

    def post(self, request, *args, **kwargs):
        # self.object = self.get_object()  # Get the lesson instance
        # print(self.form_class.forms.data())
        form = self.get_form(form_class=CourseLessonForm)  # Get the CourseLessonForm
        # problem_formset = self.get_problem_formset(post=True, lesson)

        if form.is_valid():
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
                # print("Data:", problem_formset.data)
                problem_formset.save()
            return self.form_valid(form)
        else:
            print("Invalid")
            return self.form_invalid(form)

    def form_valid(self, form):
        # problem_formset.save()
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
        self.lesson = CourseLesson.objects.get(id=kwargs["id"])
        res = super().dispatch(request, *args, **kwargs)
        if not self.lesson.id:
            print("Can not find lesson id or Delete complete")
            return HttpResponseRedirect(
                reverse(
                    "edit_course_lessons",
                    args=[self.course.slug],
                )
            )
        return res

    def get(self, request, *args, **kwargs):
        try:
            self.lesson = CourseLesson.objects.get(id=kwargs["id"])

            return super().get(request, *args, **kwargs)
        except ObjectDoesNotExist:
            raise Http404()

    def get_problem_formset(self, post=False, lesson=None):
        formset = CourseLessonProblemFormSet(
            data=self.request.POST if post else None,
            prefix=f"problems_{lesson.id}" if lesson else "problems",
            queryset=CourseLessonProblem.objects.filter(lesson=self.lesson).order_by(
                "order"
            ),
        )
        if lesson:
            for form in formset:
                form.fields["lesson"].initial = self.lesson
        return formset

    def get_context_data(self, **kwargs):
        context = super(EditCourseLessonsViewNewWindow, self).get_context_data(**kwargs)
        # get = super().get(request, *args, **kwargs)
        if self.request.method == "POST":
            print("Post Data")
            # context["formset"] = self.form_class(
            #     self.request.POST, self.request.FILES, instance=self.course
            # )
            # context["problem_formsets"] = {
            #     self.lesson.id: self.get_problem_formset(post=True, lesson=lesson.instance)
            #     for lesson in context["formset"].forms
            #     if lesson.instance.id
            # }
        else:
            context["formset"] = self.form_class(
                instance=self.course, queryset=self.course.lessons.order_by("order")
            )
            context["problem_formsets"] = {
                self.lesson.id: self.get_problem_formset(post=False, lesson=self.lesson)
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
        context["page_type"] = "edit_lesson_new"
        context["lesson_field"] = CourseLessonForm(instance=self.lesson)
        context["lesson"] = self.lesson

        return context

    def post(self, request, *args, **kwargs):
        # self.object = self.get_object()  # Get the lesson instance
        # print(self.form_class.forms.data())
        form = self.get_form(form_class=CourseLessonForm)  # Get the CourseLessonForm
        # problem_formset = self.get_problem_formset(post=True)
        # print("FORM", form.fields.keys())
        # print("FORM", form.is_valid())
        if form.is_valid():
            if "delete_lesson" in request.POST:
                form.instance.course_id = self.course.id
                form.instance.lesson_id = self.lesson.id
                self.lesson.delete()
                messages.success(request, "Lesson deleted successfully.")
                course_slug = self.course.slug
                # print("Dlelelelelelele")
                return HttpResponseRedirect(
                    reverse(
                        "edit_course_lessons",
                        args=[course_slug],
                    )
                )
            else:
                # print("Form:", form)
                form.instance.course_id = self.course.id
                form.instance.id = self.lesson.id
                # print(problem_formset)
                self.lesson = form.save()
                problem_formsets = self.get_problem_formset(
                    post=True, lesson=self.lesson
                )
                if problem_formsets.is_valid():
                    # print("Data:", problem_formsets.data)
                    problem_formsets.save()
                    for obj in problem_formsets.deleted_objects:
                        if obj.pk is not None:
                            obj.delete()
            return self.form_valid(form)
        else:
            # print("Invalid")
            return self.form_invalid(form)

    def form_valid(self, form):
        if "delete_lesson" in self.request.POST:
            # ... (your deletion logic)
            return redirect("edit_course_lessons", slug=self.course.slug)
        else:
            # ... (your form saving logic)
            return super().form_valid(form)
            # problem_formset.save()
        return super().form_valid(form)

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def get_success_url(self):
        return reverse(
            "edit_course_lessons",
            args=[self.course.slug],
        )

    # def my_view(request):
    #     # Your view logic here
    #     # context = super(EditCourseLessonsViewNewWindow, self).get_context_data(**kwargs)
    #     return render(request, 'course/edit_lesson.html')


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
        students.sort(key=lambda u: u.username.lower())
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
