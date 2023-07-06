from django.db import models
from judge.models.course import Course
from django.views.generic import ListView

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

course_directory_file = ""


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
