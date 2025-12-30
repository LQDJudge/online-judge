import os
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.utils.translation import gettext, gettext_lazy as _
from django.urls import reverse
from django.db.models import Q

from judge.models import Problem, Contest
from judge.models.profile import Organization, Profile
from judge.caching import cache_wrapper
from judge.utils.files import delete_old_image_files, generate_image_filename


def course_image_path(course, filename):
    new_filename = generate_image_filename(f"course_{course.id}", filename)
    return os.path.join(settings.DMOJ_COURSE_IMAGE_ROOT, new_filename)


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
        help_text=_("If not empty, only these organizations may see the course"),
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
    course_image = models.ImageField(
        verbose_name=_("course image"),
        upload_to=course_image_path,
        null=True,
        blank=True,
    )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("course_detail", args=(self.slug,))

    @classmethod
    def is_editable_by(cls, course, profile):
        # Admins can edit any course
        if profile and profile.user.is_superuser:
            return True

        try:
            course_role = CourseRole.objects.get(course=course, user=profile)
            return course_role.role in EDITABLE_ROLES
        except CourseRole.DoesNotExist:
            return False

    @classmethod
    def is_accessible_by(cls, course, profile):
        """Check if a user can access a course. Only enrolled users can access courses."""
        if not profile:
            return False

        if profile.user.is_superuser:
            return True

        try:
            course_role = CourseRole.objects.get(course=course, user=profile)
            return True  # Any enrolled user can access the course
        except CourseRole.DoesNotExist:
            return False  # Only enrolled users can access courses

    @classmethod
    def is_joinable(cls, course, profile):
        """Check if a user can join an open course."""
        if not profile:
            return False

        if profile.user.is_superuser:
            return False  # Admins don't need to join courses

        # User must not already be enrolled
        if CourseRole.objects.filter(course=course, user=profile).exists():
            return False

        # Course must be open for registration
        if not course.is_open:
            return False

        # Check if course is in user's organizations
        if course.organizations.exists():
            user_orgs = profile.organizations.all()
            if course.organizations.filter(id__in=user_orgs).exists():
                return course.is_public
            return False

        return course.is_public

    @classmethod
    def get_accessible_courses(cls, profile):
        """Get courses that a user can access (only enrolled courses)."""
        # Admins can access all courses
        if profile and profile.user.is_superuser:
            return Course.objects.all()

        if not profile:
            return Course.objects.none()

        # Only return courses where user has a role (is enrolled)
        return Course.objects.filter(courserole__user=profile).distinct()

    @classmethod
    def get_joinable_courses(cls, profile):
        """Get courses that a user can join (open courses they're not enrolled in)."""
        if not profile:
            return Course.objects.filter(
                is_public=True, is_open=True, organizations__isnull=True
            )

        # Get courses user is already enrolled in
        enrolled_course_ids = Course.objects.filter(
            courserole__user=profile
        ).values_list("id", flat=True)

        # Get public open courses
        public_courses = Course.objects.filter(
            is_public=True, is_open=True, organizations__isnull=True
        ).exclude(id__in=enrolled_course_ids)

        # Get organization courses for user's organizations
        user_orgs = profile.get_organization_ids()
        org_courses = Course.objects.filter(
            is_public=True, is_open=True, organizations__in=user_orgs
        ).exclude(id__in=enrolled_course_ids)

        # Combine all joinable courses
        return (public_courses | org_courses).distinct()

    @classmethod
    def get_user_courses(cls, profile):
        """Get courses where user is enrolled (has a role)"""
        if not profile:
            return Course.objects.none()

        return Course.objects.filter(courserole__user=profile).distinct()

    def _get_users_by_role(self, role):
        profile_ids = get_course_role_profile_ids(self.id, role)
        return Profile.get_cached_instances(*profile_ids)

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

    def get_lessons(self):
        return self.lessons.filter(is_visible=True).order_by("order")

    def get_contests(self):
        return (
            self.contests.select_related("contest")
            .defer("contest__description")
            .filter(contest__is_visible=True)
            .order_by("order")
        )

    def save(self, *args, **kwargs):
        # Delete old image files before saving new ones to avoid duplicates
        if self.pk:
            try:
                old_instance = Course.objects.get(pk=self.pk)
                # Check if course_image is being updated
                if self.course_image and old_instance.course_image != self.course_image:
                    delete_old_image_files(
                        settings.DMOJ_COURSE_IMAGE_ROOT,
                        f"course_{self.id}",
                    )
            except Course.DoesNotExist:
                pass
        super().save(*args, **kwargs)


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
    title = models.TextField(verbose_name=_("lesson title"))
    content = models.TextField(verbose_name=_("lesson content"))
    order = models.IntegerField(verbose_name=_("order"), default=0)
    points = models.IntegerField(verbose_name=_("points"))
    is_visible = models.BooleanField(verbose_name=_("publicly visible"), default=True)

    def get_absolute_url(self):
        return reverse(
            "course_lesson_detail",
            args=(
                self.course.slug,
                self.id,
            ),
        )

    def get_problems(self):
        """
        Get all problem IDs for this lesson in order.
        Returns a list of problem IDs.
        """
        return Problem.get_cached_instances(
            *[p["problem_id"] for p in self.get_problems_and_scores()]
        )

    @cache_wrapper(prefix="CLgps", expected_type=list)
    def get_problems_and_scores(self):
        """
        Get all problems with their scores for this lesson in order.
        Returns a list of dictionaries with problem_id and score.
        """
        return list(
            self.lesson_problems.order_by("order").values("problem_id", "score")
        )


class CourseLessonProblem(models.Model):
    lesson = models.ForeignKey(
        CourseLesson, on_delete=models.CASCADE, related_name="lesson_problems"
    )
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE)
    order = models.IntegerField(verbose_name=_("order"), default=0)
    score = models.IntegerField(verbose_name=_("score"), default=0)


class CourseContest(models.Model):
    course = models.ForeignKey(
        Course, on_delete=models.CASCADE, related_name="contests"
    )
    contest = models.ForeignKey(
        Contest, unique=True, on_delete=models.CASCADE, related_name="course"
    )
    order = models.IntegerField(verbose_name=_("order"), default=0)
    points = models.IntegerField(verbose_name=_("points"))

    def get_course_of_contest(contest):
        course_contest = contest.course.get()
        course = course_contest.course
        return course


@cache_wrapper(prefix="CRubr", expected_type=list)
def get_course_role_profile_ids(course_id, role):
    """
    Get profile IDs for users with a specific role in a course.
    This function is cached and will be invalidated when CourseRole changes.
    """
    return list(
        CourseRole.objects.filter(course_id=course_id, role=role).values_list(
            "user_id", flat=True
        )
    )
