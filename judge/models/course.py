from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext, gettext_lazy as _
from django.urls import reverse
from django.db.models import Q

from judge.models import BlogPost, Problem
from judge.models.profile import Organization, Profile


class RoleInCourse(models.TextChoices):
    STUDENT = "ST", _("Student")
    ASSISTANT = "AS", _("Assistant")
    TEACHER = "TE", _("Teacher")


EDITABLE_ROLES = (RoleInCourse.TEACHER, RoleInCourse.ASSISTANT)


class Course(models.Model):
    name = models.CharField(
        max_length=128,
        verbose_name=_("course name"),
    )
    about = models.TextField(verbose_name=_("course description"))
    is_public = models.BooleanField(
        verbose_name=_("publicly visible"),
        default=False,
    )
    organizations = models.ManyToManyField(
        Organization,
        blank=True,
        verbose_name=_("organizations"),
        help_text=_("If private, only these organizations may see the course"),
    )
    slug = models.SlugField(
        max_length=128,
        verbose_name=_("course slug"),
        help_text=_("Course name shown in URL"),
        unique=True,
        validators=[
            RegexValidator("^[-a-zA-Z0-9]+$", _("Only alphanumeric and hyphens"))
        ],
    )
    is_open = models.BooleanField(
        verbose_name=_("public registration"),
        default=False,
    )
    image_url = models.CharField(
        verbose_name=_("course image"),
        default="",
        max_length=150,
        blank=True,
    )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("course_detail", args=(self.slug,))

    @classmethod
    def is_editable_by(cls, course, profile):
        try:
            course_role = CourseRole.objects.get(course=course, user=profile)
            return course_role.role in EDITABLE_ROLES
        except CourseRole.DoesNotExist:
            return False

    @classmethod
    def is_accessible_by(cls, course, profile):
        if not profile:
            return False
        try:
            course_role = CourseRole.objects.get(course=course, user=profile)
            if course_role.course.is_public:
                return True
            return course_role.role in EDITABLE_ROLES
        except CourseRole.DoesNotExist:
            return False

    @classmethod
    def get_accessible_courses(cls, profile):
        return Course.objects.filter(
            Q(is_public=True) | Q(courserole__role__in=EDITABLE_ROLES),
            courserole__user=profile,
        ).distinct()

    def _get_users_by_role(self, role):
        course_roles = CourseRole.objects.filter(course=self, role=role).select_related(
            "user"
        )
        return [course_role.user for course_role in course_roles]

    def get_students(self):
        return self._get_users_by_role(RoleInCourse.STUDENT)

    def get_assistants(self):
        return self._get_users_by_role(RoleInCourse.ASSISTANT)

    def get_teachers(self):
        return self._get_users_by_role(RoleInCourse.TEACHER)

    @classmethod
    def add_student(cls, course, profiles):
        for profile in profiles:
            CourseRole.make_role(course=course, user=profile, role="ST")

    @classmethod
    def add_teachers(cls, course, profiles):
        for profile in profiles:
            CourseRole.make_role(course=course, user=profile, role="TE")

    @classmethod
    def add_assistants(cls, course, profiles):
        for profile in profiles:
            CourseRole.make_role(course=course, user=profile, role="AS")


class CourseRole(models.Model):
    course = models.ForeignKey(
        Course,
        verbose_name=_("course"),
        on_delete=models.CASCADE,
        db_index=True,
    )
    user = models.ForeignKey(
        Profile,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        related_name="course_roles",
    )

    role = models.CharField(
        max_length=2,
        choices=RoleInCourse.choices,
        default=RoleInCourse.STUDENT,
    )

    @classmethod
    def make_role(self, course, user, role):
        userqueryset = CourseRole.objects.filter(course=course, user=user)
        if userqueryset.exists():
            userqueryset[0].role = role
        else:
            couresrole = CourseRole()
            couresrole.course = course
            couresrole.user = user
            couresrole.role = role
            couresrole.save()

    class Meta:
        unique_together = ("course", "user")


class CourseLesson(models.Model):
    course = models.ForeignKey(
        Course,
        verbose_name=_("course"),
        related_name="lessons",
        on_delete=models.CASCADE,
    )
    title = models.TextField(verbose_name=_("course title"))
    content = models.TextField(verbose_name=_("course content"))
    problems = models.ManyToManyField(Problem, verbose_name=_("problem"), blank=True)
    order = models.IntegerField(verbose_name=_("order"), default=0)
    points = models.IntegerField(verbose_name=_("points"))


class CourseLessonProblem(models.Model):
    lesson = models.ForeignKey(
        CourseLesson, on_delete=models.CASCADE, related_name="lesson_problems"
    )
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE)
    order = models.IntegerField(verbose_name=_("order"), default=0)
    score = models.IntegerField(verbose_name=_("score"), default=0)
