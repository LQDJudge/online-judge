import os
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.urls import reverse

from judge.models import Problem, Contest
from judge.models.profile import Organization, Profile
from judge.caching import cache_wrapper

from judge.utils.files import generate_secure_filename


def course_image_path(course, filename):
    new_filename = generate_secure_filename(filename, f"course_{course.id}")
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

        return CourseRole.objects.filter(
            course=course, user=profile
        ).exists()  # Only enrolled users can access courses

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

    needs_progress_recalculation = models.BooleanField(
        default=True,
        verbose_name=_("needs progress recalculation"),
        help_text=_(
            "Whether the user's lesson progress and unlock states need to be recalculated"
        ),
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
    MAX_LESSONS_PER_COURSE = 100

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

    def clean(self):
        super().clean()
        # Check if course is set before accessing it
        if not hasattr(self, "course_id") or not self.course_id:
            return

        # Limit the number of lessons per course
        if not self.pk:  # Only check on creation
            lesson_count = CourseLesson.objects.filter(course_id=self.course_id).count()
            if lesson_count >= self.MAX_LESSONS_PER_COURSE:
                raise ValidationError(
                    _("A course cannot have more than %(max)s lessons.")
                    % {"max": self.MAX_LESSONS_PER_COURSE}
                )

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

    def save(self, *args, **kwargs):
        is_new = not self.pk

        # For new lessons, set order to max + 1 if not specified or invalid
        if is_new and hasattr(self, "course_id") and self.course_id:
            if self.order < 1:
                max_order = (
                    CourseLesson.objects.filter(course_id=self.course_id).aggregate(
                        models.Max("order")
                    )["order__max"]
                    or 0
                )
                self.order = max_order + 1

        # Track if points changed (affects grades)
        if self.pk:
            try:
                old_instance = CourseLesson.objects.get(pk=self.pk)
                points_changed = old_instance.points != self.points
            except CourseLesson.DoesNotExist:
                points_changed = False
        else:
            points_changed = False

        super().save(*args, **kwargs)

        # Mark for recalculation if points changed
        if points_changed:
            from judge.utils.course_prerequisites import mark_course_for_recalculation

            mark_course_for_recalculation(self.course)

    def delete(self, *args, **kwargs):
        course = self.course
        super().delete(*args, **kwargs)
        # Mark all users for recalculation when lesson is deleted
        from judge.utils.course_prerequisites import mark_course_for_recalculation

        mark_course_for_recalculation(course)


class CourseLessonProblem(models.Model):
    lesson = models.ForeignKey(
        CourseLesson, on_delete=models.CASCADE, related_name="lesson_problems"
    )
    problem = models.ForeignKey(
        Problem, verbose_name=_("problem"), on_delete=models.CASCADE
    )
    order = models.IntegerField(verbose_name=_("order"), default=0)
    score = models.IntegerField(verbose_name=_("score"), default=0)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Mark all users for recalculation when lesson content changes
        from judge.utils.course_prerequisites import mark_course_for_recalculation

        mark_course_for_recalculation(self.lesson.course)

    def delete(self, *args, **kwargs):
        course = self.lesson.course
        super().delete(*args, **kwargs)
        # Mark all users for recalculation when lesson content changes
        from judge.utils.course_prerequisites import mark_course_for_recalculation

        mark_course_for_recalculation(course)


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


class CourseLessonPrerequisite(models.Model):
    """
    Prerequisite edge: user needs required_percentage% in lesson at source_order
    to unlock lesson at target_order.
    Uses order (integer) instead of ForeignKey for simplicity.
    """

    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="lesson_prerequisites",
        verbose_name=_("course"),
    )
    source_order = models.IntegerField(
        verbose_name=_("source lesson order"),
        help_text=_("Order of the prerequisite lesson"),
    )
    target_order = models.IntegerField(
        verbose_name=_("target lesson order"),
        help_text=_("Order of the lesson to unlock"),
    )
    required_percentage = models.FloatField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        verbose_name=_("required percentage"),
        help_text=_("Minimum grade percentage required in source lesson (0-100)"),
    )

    class Meta:
        unique_together = ("course", "source_order", "target_order")
        verbose_name = _("Lesson Prerequisite")
        verbose_name_plural = _("Lesson Prerequisites")

    def clean(self):
        super().clean()
        if self.source_order >= self.target_order:
            raise ValidationError(
                _("Source lesson order must be less than target lesson order.")
            )

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Mark all users for recalculation when prerequisites change
        from judge.utils.course_prerequisites import mark_course_for_recalculation

        mark_course_for_recalculation(self.course)

    def delete(self, *args, **kwargs):
        course = self.course
        super().delete(*args, **kwargs)
        # Mark all users for recalculation when prerequisite is deleted
        from judge.utils.course_prerequisites import mark_course_for_recalculation

        mark_course_for_recalculation(course)

    def __str__(self):
        return f"{self.course.slug}: Lesson {self.source_order} -> Lesson {self.target_order} ({self.required_percentage}%)"


class CourseLessonProgress(models.Model):
    """
    Tracks user (Profile) progress and unlock state for each lesson.
    """

    user = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name="lesson_progress",
        verbose_name=_("user"),
    )
    lesson = models.ForeignKey(
        CourseLesson,
        on_delete=models.CASCADE,
        related_name="user_progress",
        verbose_name=_("lesson"),
    )
    is_unlocked = models.BooleanField(
        default=False,
        verbose_name=_("is unlocked"),
        help_text=_("Whether the user has unlocked this lesson"),
    )
    percentage = models.FloatField(
        default=0,
        verbose_name=_("percentage"),
        help_text=_("User's grade percentage for this lesson"),
    )

    class Meta:
        unique_together = ("user", "lesson")
        verbose_name = _("Lesson Progress")
        verbose_name_plural = _("Lesson Progress Records")

    def __str__(self):
        status = "unlocked" if self.is_unlocked else "locked"
        return f"{self.user.user.username} - {self.lesson.title} ({status}, {self.percentage:.1f}%)"


class BestSubmission(models.Model):
    """
    Caches the best submission for each user/problem pair.
    Updated when a new submission is judged and has a better score.
    """

    user = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name="best_submissions",
        verbose_name=_("user"),
    )
    problem = models.ForeignKey(
        Problem,
        on_delete=models.CASCADE,
        related_name="best_submissions",
        verbose_name=_("problem"),
    )
    submission = models.ForeignKey(
        "Submission",
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("best submission"),
    )
    points = models.FloatField(
        default=0,
        verbose_name=_("points"),
        help_text=_("Best score achieved (case_points)"),
    )
    case_total = models.FloatField(
        default=0,
        verbose_name=_("case total"),
        help_text=_("Total possible points for this problem"),
    )

    class Meta:
        unique_together = ("user", "problem")
        verbose_name = _("Best Submission")
        verbose_name_plural = _("Best Submissions")
        indexes = [
            models.Index(fields=["user", "problem"]),
        ]

    def __str__(self):
        return f"{self.user.user.username} - {self.problem.code}: {self.points}/{self.case_total}"

    def save(self, *args, **kwargs):
        # Track if points changed for triggering lesson grade updates
        old_points = 0
        if self.pk:
            try:
                old_instance = BestSubmission.objects.get(pk=self.pk)
                old_points = old_instance.points
            except BestSubmission.DoesNotExist:
                pass

        super().save(*args, **kwargs)

        # If points changed, trigger lesson grade update for related lessons
        if abs(self.points - old_points) > 0.001:
            self._update_related_lesson_grades()

    def _update_related_lesson_grades(self):
        """Update lesson grades for lessons containing this problem."""
        from judge.utils.course_prerequisites import update_lesson_grade

        # Find all lessons containing this problem
        lesson_problems = CourseLessonProblem.objects.filter(
            problem=self.problem
        ).select_related("lesson__course")

        for lesson_problem in lesson_problems:
            lesson = lesson_problem.lesson
            course = lesson.course

            # Check if user is enrolled in this course
            if CourseRole.objects.filter(course=course, user=self.user).exists():
                update_lesson_grade(self.user, lesson)

    @classmethod
    def update_from_submission(cls, submission):
        """
        Recalculate best submission for a user/problem after a submission is judged.

        Args:
            submission: Submission object that was just judged

        Returns:
            BestSubmission object if updated/created, None if no valid submissions
        """
        if submission.status != "D":  # Only consider completed submissions
            return None

        return cls.recalculate_for_user_problem(
            submission.user_id, submission.problem_id
        )

    @classmethod
    def recalculate_for_user_problem(cls, user_id, problem_id):
        """
        Recalculate best submission for a user/problem pair.
        Called after a submission is deleted to find the new best submission.

        Args:
            user_id: Profile ID of the user
            problem_id: Problem ID
        """
        from judge.models import Submission

        # Find the best remaining submission for this user/problem
        best_submission = (
            Submission.objects.filter(
                user_id=user_id,
                problem_id=problem_id,
                status="D",
            )
            .order_by("-case_points", "-date")
            .first()
        )

        if best_submission:
            # Update or create best submission record
            best_sub, created = cls.objects.update_or_create(
                user_id=user_id,
                problem_id=problem_id,
                defaults={
                    "submission": best_submission,
                    "points": best_submission.case_points or 0,
                    "case_total": best_submission.case_total or 0,
                },
            )
            return best_sub
        else:
            # No submissions left, delete the best submission record if it exists
            cls.objects.filter(user_id=user_id, problem_id=problem_id).delete()
            return None
