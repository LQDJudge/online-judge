from django.db import models
from judge.models.course import Course , CourseAssignment , CourseRole
from judge.models.contest import ContestParticipation, Contest
from django.views.generic import ListView, DetailView
from django.utils import timezone

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

class CourseDetail(ListView):
    model = CourseAssignment
    template_name = "course/course.html"

    def get_queryset(self):
        cur_course = Course.objects.get(slug=self.kwargs['slug'])
        return CourseAssignment.objects.filter(course=cur_course)

    def get_context_data(self, **kwargs):
        context = super(CourseDetail, self).get_context_data(**kwargs)
        context['slug'] = self.kwargs['slug']
        return context

def best_score_user_contest(user , contest):
    participated_contests = ContestParticipation.objects.filter(user=user, contest=contest)
    progress_point = 0
    for cur_contest in participated_contests:
        progress_point = max( progress_point , cur_contest.score_final )
    return progress_point

def progress_contest(user , contest):
    return best_score_user_contest(user, contest) / contest.total_points

def progress_course(user , course):
    assignments = CourseAssignment.objects.filter(course=course)
    assignments_total_point = 0
    for assignment in assignments:
        assignments_total_point += assignment.points
    progress = 0
    for assignment in assignments:
        progress += progress_contest(user,assignment.contest) * assignment.points / assignments_total_point
    return progress
    
class CourseStudentResults(ListView):
    model = ContestParticipation
    template_name = "course/grades.html"

    def get_queryset(self):
        cur_course = Course.objects.get(slug=self.kwargs['slug'])
        contests = CourseAssignment.objects.filter(course=cur_course)
        students = Course.get_students(cur_course)
        grades_table = []
        for student in students:
            grades_student = [student.user.user.username]
            for contest in contests:
                grades_student.append( round(progress_contest(student.user, contest.contest) * 100 ) )
            grades_student.append( round(progress_course(student.user, cur_course) * 100) )
            grades_table.append( grades_student )
        return grades_table
    
    def get_context_data(self, **kwargs):
        context = super(CourseStudentResults, self).get_context_data(**kwargs)
        cur_course = Course.objects.get(slug=self.kwargs['slug'])
        contests = CourseAssignment.objects.filter(course=cur_course)
        context['contests'] = contests
        return context

