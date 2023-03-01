from django.forms import ModelForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic.edit import UpdateView
from judge.models.course import Course, CourseResource
from django.views.generic import ListView, UpdateView, DetailView
from judge.views.feed import FeedView
from django.http import (
    Http404,
    HttpResponsePermanentRedirect,
    HttpResponseRedirect,
)

from django.shortcuts import get_object_or_404
from django.utils.translation import gettext as _
from judge.utils.views import (
    generic_message,
)
from django.urls import reverse_lazy
from django.contrib import messages

__all__ = [
    "CourseList",
    "CourseDetail",
    "CourseResource",
    "CourseResourceDetail",
    "CourseStudentResults",
    "CourseEdit",
    "CourseResourceDetailEdit",
    "CourseResourceEdit",
]


class CourseBase(object):
    def is_editable_by(self, course=None):
        if course is None:
            course = self.object
        if self.request.profile:
            return Course.is_editable_by(course, self.request.profile)
        return False

    def is_accessible_by(self, course):
        if course is None:
            course = self.object
        if self.request.profile:
            return Course.is_accessible_by(course, self.request.profile)
        return False


class CourseMixin(CourseBase):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = self.is_editable_by(self.course)
        context["can access"] = self.is_accessible_by(self.course)
        context["course"] = self.course
        return context

    def dispatch(self, request, *args, **kwargs):
        print(self)
        try:
            self.course_id = int(kwargs["pk"])
            self.course = get_object_or_404(Course, id=self.course_id)
        except Http404:
            key = None
            if hasattr(self, "slug_url_kwarg"):
                key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(
                    request,
                    _("No such course"),
                    _('Could not find a course with the key "%s".') % key,
                )
            else:
                return generic_message(
                    request,
                    _("No such course"),
                    _("Could not find such course."),
                )
        if self.course.slug != kwargs["slug"]:
            return HttpResponsePermanentRedirect(
                request.get_full_path().replace(kwargs["slug"], self.course.slug)
            )

        return super(CourseMixin, self).dispatch(request, *args, **kwargs)


class CourseHomeView(CourseMixin):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if not hasattr(self, "course"):
            self.course = self.object
        return context


class CourseHome(CourseHomeView, FeedView):
    template_name = "course/home.html"

    def get_queryset(self):
        return CourseResource.objects.filter(
            is_public=True,
            course=self.course,
        ).order_by("order")

    def get_context_data(self, **kwargs):
        context = super(CourseHome, self).get_context_data(**kwargs)
        context["title"] = self.course.name
        context["description"] = self.course.about
        return context


class CourseResourceList(CourseMixin, ListView):
    template_name = "course/resource.html"

    def get_queryset(self):
        return CourseResource.objects.filter(
            is_public=True,
            course=self.course,
        ).order_by("order")

    def get_context_data(self, **kwargs):
        context = super(CourseResourceList, self).get_context_data(**kwargs)
        context["title"] = self.course.name

        return context


class CourseResouceDetail(DetailView):
    template_name = "course/resource-content.html"
    model = CourseResource

    def get_context_data(self, **kwargs):
        context = super(CourseResouceDetail, self).get_context_data(**kwargs)
        return context


class CourseAdminMixin(CourseMixin):
    def dispatch(self, request, *args, **kwargs):
        res = super(CourseAdminMixin, self).dispatch(request, *args, **kwargs)
        if not hasattr(self, "course") or self.is_editable_by(self.course):
            return res
        return generic_message(
            request,
            _("Can't edit course"),
            _("You are not allowed to edit this course."),
            status=403,
        )


class CourseResourceDetailEditForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(CourseResourceDetailEditForm, self).__init__(*args, **kwargs)


class CourseResourceDetailEdit(LoginRequiredMixin, UpdateView):
    template_name = "course/resource_detail_edit.html"
    model = CourseResource
    fields = ["description", "files", "is_public"]

    def get_success_url(self):
        return self.request.get_full_path()

    def form_valid(self, form):
        form.save()
        return super().form_valid(form)


class CourseResourceEdit(CourseMixin, LoginRequiredMixin, ListView):
    template_name = "course/resource_edit.html"

    def get_queryset(self):
        return CourseResource.objects.filter(
            course=self.course,
        ).order_by("order")

    def post(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        for resource in queryset:
            if request.POST.get("resource-" + str(resource.pk) + "-delete") != None:
                resource.delete()
            else:
                if request.POST.get("resource-" + str(resource.pk) + "-public") != None:
                    resource.is_public = True
                else:
                    resource.is_public = False
                resource.order = request.POST.get(
                    "resource-" + str(resource.pk) + "-order"
                )
                resource.save()
        return HttpResponseRedirect(request.path)

    def get_context_data(self, **kwargs):
        return super(CourseResourceEdit, self).get_context_data(**kwargs)


class CourseListMixin(object):
    def get_queryset(self):
        return Course.objects.filter(is_open="true").values()


class CourseList(ListView):
    model = Course
    template_name = "course/list.html"
    queryset = Course.objects.filter(is_public=True).filter(is_open=True)

    def get_context_data(self, **kwargs):
        context = super(CourseList, self).get_context_data(**kwargs)
        available, enrolling = [], []
        for course in Course.objects.filter(is_public=True).filter(is_open=True):
            if Course.is_accessible_by(course, self.request.profile):
                enrolling.append(course)
            else:
                available.append(course)
        context["available"] = available
        context["enrolling"] = enrolling
        return context
