from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext, gettext_lazy as _

from judge.models import Contest
from judge.models.profile import Organization, Profile

__all__ = [
    "Course",
    "CourseRole",
    "CourseResource",
    "CourseAssignment",
]

course_directory_file = ""


class Course(models.Model):
    name = models.CharField(
        max_length=128,
        verbose_name=_("course name"),
    )
    about = models.TextField(verbose_name=_("organization description"))
    ending_time = models.DateTimeField(
        verbose_name=_("ending time"),
    )
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

    @classmethod
    def is_editable_by(course, profile):
        if profile.is_superuser:
            return True
        userquery = CourseRole.objects.filter(course=course, user=profile)
        if userquery.exists():
            if userquery[0].role == "AS" or userquery[0].role == "TE":
                return True
        return False

    @classmethod
    def is_accessible_by(cls, course, profile):
        userqueryset = CourseRole.objects.filter(course=course, user=profile)
        if userqueryset.exists():
            return True
        else:
            return False

    @classmethod
    def get_students(cls, course):
        return CourseRole.objects.filter(course=course, role="ST").values("user")

    @classmethod
    def get_assistants(cls, course):
        return CourseRole.objects.filter(course=course, role="AS").values("user")

    @classmethod
    def get_teachers(cls, course):
        return CourseRole.objects.filter(course=course, role="TE").values("user")

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
    course = models.OneToOneField(
        Course,
        verbose_name=_("course"),
        on_delete=models.CASCADE,
        db_index=True,
    )
    user = models.ForeignKey(
        Profile,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        related_name=_("user_of_course"),
    )

    class RoleInCourse(models.TextChoices):
        STUDENT = "ST", _("Student")
        ASSISTANT = "AS", _("Assistant")
        TEACHER = "TE", _("Teacher")

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


class CourseResource(models.Model):
    course = models.OneToOneField(
        Course,
        verbose_name=_("course"),
        on_delete=models.CASCADE,
        db_index=True,
    )
    files = models.FileField(
        verbose_name=_("course files"),
        null=True,
        blank=True,
        upload_to=course_directory_file,
    )
    description = models.CharField(
        verbose_name=_("description"),
        blank=True,
        max_length=150,
    )
    order = models.IntegerField(null=True, default=None)
    is_public = models.BooleanField(
        verbose_name=_("publicly visible"),
        default=False,
    )


class CourseAssignment(models.Model):
    course = models.OneToOneField(
        Course,
        verbose_name=_("course"),
        on_delete=models.CASCADE,
        db_index=True,
    )
    contest = models.OneToOneField(
        Contest,
        verbose_name=_("contest"),
        on_delete=models.CASCADE,
    )
    points = models.FloatField(
        verbose_name=_("points"),
    )
