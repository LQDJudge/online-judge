import os
import uuid
import json
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from judge.models.profile import Profile
from judge.models.contest import Contest, ContestParticipation
from judge.models.course import CourseLesson, Course


def quiz_answer_file_path(instance, filename):
    """Generate unique path for quiz answer file uploads"""
    ext = filename.split(".")[-1]
    filename = f"{uuid.uuid4()}.{ext}"
    return os.path.join("quiz_answers", str(instance.answer.attempt.id), filename)


class QuizQuestionType(models.TextChoices):
    MULTIPLE_CHOICE = "MC", _("Multiple Choice")
    MULTIPLE_ANSWER = "MA", _("Multiple Answer")
    SHORT_ANSWER = "SA", _("Short Answer")
    ESSAY = "ES", _("Essay")
    TRUE_FALSE = "TF", _("True/False")


class QuizQuestion(models.Model):
    question_type = models.CharField(
        max_length=2, choices=QuizQuestionType.choices, verbose_name=_("Question Type")
    )

    title = models.CharField(
        max_length=255,
        verbose_name=_("Question Title"),
        help_text=_("Brief title for question management"),
    )

    content = models.TextField(
        verbose_name=_("Question Content"),
        help_text=_("The actual question text shown to students"),
    )

    # Choices for MC/MA/TF questions
    # Format: [{"id": "a", "text": "Option A"}, {"id": "b", "text": "Option B"}, ...]
    # For TF: [{"id": "true", "text": "True"}, {"id": "false", "text": "False"}]
    choices = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("Choices"),
        help_text=_("Answer choices for MC/MA/TF questions"),
    )

    # Correct answers
    # For MC: {"answers": "b"} - single correct choice ID
    # For MA: {"answers": ["a", "c"]} - list of correct choice IDs
    # For TF: {"answers": "true"} or {"answers": "false"}
    # For SA: {"type": "exact"|"regex", "answers": ["5", "five"], "case_sensitive": false}
    # For ES: null (essay questions don't have predefined answers)
    correct_answers = models.JSONField(
        null=True,
        blank=True,
        verbose_name=_("Correct Answers"),
        help_text=_("Correct answer(s) for the question"),
    )

    # Default point value for this question
    default_points = models.FloatField(
        default=1.0,
        validators=[MinValueValidator(0)],
        verbose_name=_("Default Points"),
        help_text=_("Default point value when added to quizzes"),
    )

    # Public visibility
    is_public = models.BooleanField(
        default=False,
        verbose_name=_("Is Public"),
        help_text=_("Whether this question is publicly visible"),
    )

    # Explanation shown after quiz completion
    explanation = models.TextField(
        blank=True,
        verbose_name=_("Explanation"),
        help_text=_("Explanation shown to students after completion"),
    )

    # Shuffle choices for this question
    shuffle_choices = models.BooleanField(
        default=False,
        verbose_name=_("Shuffle Choices"),
        help_text=_("Randomize choice order for this question"),
    )

    # Tags for categorization - stored as comma-separated string for simplicity and searchability
    tags = models.CharField(
        max_length=500,
        blank=True,
        verbose_name=_("Tags"),
        help_text=_("Comma-separated tags for categorizing questions"),
    )

    authors = models.ManyToManyField(
        Profile,
        verbose_name=_("authors"),
        blank=True,
        related_name="authored_quiz_questions",
        help_text=_(
            "These users will be able to edit the question and be listed as authors"
        ),
    )

    curators = models.ManyToManyField(
        Profile,
        verbose_name=_("curators"),
        blank=True,
        related_name="curated_quiz_questions",
        help_text=_(
            "These users will be able to edit the question, but not be listed as authors"
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Quiz Question")
        verbose_name_plural = _("Quiz Questions")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["question_type"]),
            models.Index(fields=["is_public"]),
        ]

    def __str__(self):
        return f"Q{self.pk}: {self.title}" if self.pk else self.title

    def is_editor(self, profile):
        """Check if profile is an editor (author or curator)"""
        return (
            self.authors.filter(id=profile.id) | self.curators.filter(id=profile.id)
        ).exists()

    def is_editable_by(self, user):
        """Check if user can edit (editor or superuser)"""
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return self.is_editor(user.profile)

    def get_tags_list(self):
        """Return tags as a list by splitting on comma"""
        if not self.tags:
            return []
        return [tag.strip() for tag in self.tags.split(",") if tag.strip()]

    def has_tag(self, tag):
        """Check if question has a specific tag"""
        return tag.lower() in [t.lower() for t in self.get_tags_list()]

    def get_choices_for_attempt(self, seed=None):
        """
        Get choices for this question, shuffled if configured.

        Args:
            seed: Random seed for consistent shuffling per attempt

        Returns:
            List of choices, shuffled if shuffle_choices is True
        """
        if not self.choices or not isinstance(self.choices, list):
            return self.choices

        if not self.shuffle_choices:
            return self.choices

        # Use seed for consistent shuffling per attempt
        import random

        choices_copy = self.choices.copy()
        if seed is not None:
            random.Random(seed).shuffle(choices_copy)
        else:
            random.shuffle(choices_copy)
        return choices_copy

    def auto_grade(self):
        """
        Re-grade all quiz attempts that include this question.
        """
        # Get all answers for this question across all attempts
        answers_to_regrade = QuizAnswer.objects.filter(
            question=self, attempt__is_submitted=True
        )

        updated_count = 0
        for answer in answers_to_regrade:
            # Each answer has its own auto_grade method
            if answer.auto_grade():
                updated_count += 1

        return updated_count


class Quiz(models.Model):
    """Collection of questions forming a quiz"""

    code = models.SlugField(
        max_length=20,
        unique=True,
        validators=[RegexValidator("^[a-z0-9]+$", _("Quiz code must be ^[a-z0-9]+$"))],
        verbose_name=_("quiz code"),
        help_text=_("A short, unique code for the quiz, used in the URL after /quiz/"),
    )

    title = models.CharField(
        max_length=255,
        verbose_name=_("Quiz Title"),
        db_index=True,
        help_text=_("The full title of the quiz, as shown in the quiz list"),
    )

    description = models.TextField(
        blank=True,
        verbose_name=_("Description"),
        help_text=_("Instructions or description shown before starting"),
    )

    # Time limit in minutes, 0 means no limit
    time_limit = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name=_("Time Limit (minutes)"),
        help_text=_("0 for no time limit"),
    )

    # Quiz configuration
    shuffle_questions = models.BooleanField(
        default=False,
        verbose_name=_("Shuffle Questions"),
        help_text=_("Randomize question order for each attempt"),
    )

    is_shown_answer = models.BooleanField(
        default=False,
        verbose_name=_("Show Answers"),
        help_text=_(
            "Whether answers and explanations are shown to students after submission"
        ),
    )

    # Questions linked via M2M relationship
    questions = models.ManyToManyField(
        QuizQuestion,
        through="QuizQuestionAssignment",
        related_name="quizzes",
        verbose_name=_("Questions"),
    )

    # Multiple creators support - similar to Problem model
    authors = models.ManyToManyField(
        Profile,
        verbose_name=_("authors"),
        blank=True,
        related_name="authored_quizzes",
        help_text=_(
            "These users will be able to edit the quiz and be listed as authors"
        ),
    )

    curators = models.ManyToManyField(
        Profile,
        verbose_name=_("curators"),
        blank=True,
        related_name="curated_quizzes",
        help_text=_(
            "These users will be able to edit the quiz, but not be listed as authors"
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Created At"))

    class Meta:
        verbose_name = _("Quiz")
        verbose_name_plural = _("Quizzes")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code} - {self.title}" if self.code else self.title

    def get_absolute_url(self):
        """Get the URL for this quiz"""
        from django.urls import reverse

        return reverse("quiz:detail", args=[self.code])

    def is_editor(self, profile):
        """Check if profile is an editor (author or curator)"""
        return (
            self.authors.filter(id=profile.id) | self.curators.filter(id=profile.id)
        ).exists()

    def is_editable_by(self, user):
        """Check if user can edit (editor or superuser)"""
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return self.is_editor(user.profile)

    def is_accessible_by(self, user):
        """
        Check if user can access this quiz.

        Returns True if:
        - User is superuser
        - User is an editor (author/curator)
        - Quiz is associated with a course the user is enrolled in
        - Quiz is part of a contest the user can access
        """
        if not user or not user.is_authenticated:
            return False

        if user.is_superuser:
            return True

        if self.is_editor(user.profile):
            return True

        # Check if quiz is in a course lesson the user has access to
        from judge.models.course import Course

        lesson_quizzes = CourseLessonQuiz.objects.filter(quiz=self, is_visible=True)
        for lesson_quiz in lesson_quizzes:
            course = lesson_quiz.lesson.course
            if Course.is_accessible_by(course, user.profile):
                return True

        # Check if quiz is in a contest the user can access
        if hasattr(self, "contest_quizzes") and self.contest_quizzes.exists():
            for contest_quiz in self.contest_quizzes.all():
                if contest_quiz.contest.is_accessible_by(user):
                    return True

        return False

    def get_questions(self):
        """
        Get all questions for this quiz in order.
        Returns QuerySet of QuizQuestionAssignment with related questions.
        """
        return self.quiz_questions.select_related("question").order_by("order")

    def get_total_points(self):
        """Calculate total points for this quiz"""
        return self.quiz_questions.aggregate(total=models.Sum("points"))["total"] or 0

    def show_answers(self, user):
        """
        Check if answers and explanations should be shown to a user.

        Returns True if:
        - User is superuser
        - User is an editor of this quiz
        - is_shown_answer is True and user has submitted the quiz
        """
        if not user or not user.is_authenticated:
            return False

        if user.is_superuser:
            return True

        if hasattr(user, "profile") and self.is_editor(user.profile):
            return True

        if self.is_shown_answer:
            # Check if user has a submitted attempt
            return QuizAttempt.objects.filter(
                user=user.profile, quiz=self, is_submitted=True
            ).exists()

        return False


class QuizQuestionAssignment(models.Model):
    """
    M2M relationship between Quiz and QuizQuestion.
    Allows same question to have different points in different quizzes.
    """

    quiz = models.ForeignKey(
        Quiz, on_delete=models.CASCADE, related_name="quiz_questions"
    )

    question = models.ForeignKey(
        QuizQuestion, on_delete=models.CASCADE, related_name="question_assignments"
    )

    points = models.IntegerField(
        default=1,
        validators=[MinValueValidator(0)],
        verbose_name=_("Points"),
        help_text=_("Points for this question in this quiz"),
    )

    order = models.IntegerField(
        default=0,
        verbose_name=_("Order"),
        help_text=_("Display order in quiz (if not shuffled)"),
    )

    class Meta:
        verbose_name = _("Quiz Question Assignment")
        verbose_name_plural = _("Quiz Question Assignments")
        unique_together = ("quiz", "question")
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.quiz.title} - {self.question.title} ({self.points} pts)"


class CourseLessonQuiz(models.Model):
    """Links quizzes to course lessons"""

    lesson = models.ForeignKey(
        CourseLesson, on_delete=models.CASCADE, related_name="lesson_quizzes"
    )

    quiz = models.ForeignKey(
        Quiz, on_delete=models.CASCADE, related_name="course_lessons"
    )

    max_attempts = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name=_("Max Attempts"),
        help_text=_("0 for unlimited attempts"),
    )

    # Points for completing this quiz in the lesson context
    points = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name=_("Lesson Points"),
        help_text=_("Points awarded in lesson for completing quiz"),
    )

    order = models.IntegerField(
        default=0, verbose_name=_("Order"), help_text=_("Display order in lesson")
    )

    is_visible = models.BooleanField(default=True, verbose_name=_("Visible"))

    class Meta:
        verbose_name = _("Course Lesson Quiz")
        verbose_name_plural = _("Course Lesson Quizzes")
        unique_together = ("lesson", "quiz")
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.lesson.title} - {self.quiz.title}"

    def get_attempts_count(self, user):
        """Count user's attempts for this quiz in this lesson"""
        if not user or not user.is_authenticated:
            return 0
        return QuizAttempt.objects.filter(
            user=user.profile, quiz=self.quiz, lesson_quiz=self
        ).count()

    def get_best_score(self, user):
        """Get user's highest score across all attempts for this quiz in this lesson"""
        if not user or not user.is_authenticated:
            return None
        best_attempt = (
            QuizAttempt.objects.filter(
                user=user.profile, quiz=self.quiz, lesson_quiz=self, is_submitted=True
            )
            .order_by("-score")
            .first()
        )
        return best_attempt.score if best_attempt else None

    def can_attempt(self, user):
        """
        Check if user can start a new attempt for this quiz in this lesson.

        Returns True if:
        - Quiz is visible
        - User has access to the course
        - max_attempts is 0 (unlimited) or user hasn't exceeded limit
        """
        if not user or not user.is_authenticated:
            return False

        if not self.is_visible:
            return False

        # Check course access
        if not Course.is_accessible_by(self.lesson.course, user.profile):
            return False

        # Check attempt limit
        if self.max_attempts > 0:
            attempts_count = self.get_attempts_count(user)
            if attempts_count >= self.max_attempts:
                return False

        return True


class QuizAttempt(models.Model):
    """Tracks individual quiz attempts by users"""

    user = models.ForeignKey(
        Profile, on_delete=models.CASCADE, related_name="quiz_attempts"
    )

    quiz = models.ForeignKey(Quiz, on_delete=models.CASCADE, related_name="attempts")

    # Link to contest participation if this is a contest quiz
    contest_participation = models.ForeignKey(
        ContestParticipation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="quiz_attempts",
    )

    # Link to lesson quiz if this is a lesson quiz
    lesson_quiz = models.ForeignKey(
        CourseLessonQuiz,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attempts",
    )

    # Attempt number for this user/quiz combination (1, 2, 3, etc.)
    attempt_number = models.IntegerField(default=1, verbose_name=_("Attempt Number"))

    start_time = models.DateTimeField(auto_now_add=True, verbose_name=_("Start Time"))

    end_time = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("End Time"),
        help_text=_("When submitted or timed out"),
    )

    # Store the actual time limit for this attempt (in case quiz settings change)
    time_limit_minutes = models.IntegerField(
        default=0, verbose_name=_("Time Limit (minutes)")
    )

    is_submitted = models.BooleanField(default=False, verbose_name=_("Is Submitted"))

    # Scoring
    score = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True, verbose_name=_("Score")
    )

    max_score = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Max Score"),
    )

    class Meta:
        verbose_name = _("Quiz Attempt")
        verbose_name_plural = _("Quiz Attempts")
        ordering = ["-start_time"]
        indexes = [
            models.Index(fields=["user", "quiz"]),
            models.Index(fields=["quiz", "is_submitted"]),
        ]

    def __str__(self):
        return f"{self.user.user.username} - {self.quiz.title} - Attempt #{self.attempt_number}"

    @property
    def duration(self):
        """
        Get the duration of this attempt.
        Returns timedelta if submitted, None otherwise.
        """
        if self.end_time and self.start_time:
            return self.end_time - self.start_time
        return None

    def time_remaining(self):
        """
        Calculate remaining time for this attempt in seconds.
        Returns None if no time limit, 0 if expired or submitted.
        """
        if not self.time_limit_minutes:
            return None

        if self.is_submitted:
            return 0

        elapsed = (timezone.now() - self.start_time).total_seconds()
        remaining = (self.time_limit_minutes * 60) - elapsed
        return max(0, remaining)

    def is_expired(self):
        """Check if attempt has exceeded time limit"""
        if not self.time_limit_minutes or self.is_submitted:
            return False

        time_elapsed = timezone.now() - self.start_time
        return time_elapsed.total_seconds() > (self.time_limit_minutes * 60)

    def calculate_score(self):
        """Calculate and update the score for this attempt"""
        # Sum points from all answers
        total_score = sum(answer.points for answer in self.answers.all())

        # Get max score from quiz
        max_score = self.quiz.get_total_points()

        self.score = total_score
        self.max_score = max_score
        self.save(update_fields=["score", "max_score"])

        return total_score, max_score

    def auto_submit(self):
        """
        Auto-submit this attempt when time expires.
        Grades all answers and marks the attempt as submitted.
        """
        if self.is_submitted:
            return

        self.is_submitted = True
        self.end_time = timezone.now()
        self.save(update_fields=["is_submitted", "end_time"])

        # Auto-grade all answers
        for answer in self.answers.all():
            answer.auto_grade()

        # Calculate the final score
        self.calculate_score()


class QuizAnswer(models.Model):
    """
    Individual answer to a quiz question.
    Unified answer field stores all types of answers as JSON/text.
    """

    attempt = models.ForeignKey(
        QuizAttempt, on_delete=models.CASCADE, related_name="answers"
    )

    question = models.ForeignKey(
        QuizQuestion, on_delete=models.CASCADE, related_name="answers"
    )

    # Unified answer field - stores different types:
    # - MC: single choice ID (string)
    # - MA: list of choice IDs (JSON array)
    # - TF: 'true' or 'false' (string)
    # - SA: text answer (string)
    # - ES: essay text (long string)
    answer = models.TextField(
        blank=True,
        verbose_name=_("Answer"),
        help_text=_("Student's answer (format depends on question type)"),
    )

    # Grading fields
    points = models.FloatField(
        default=0,
        validators=[MinValueValidator(0)],
        verbose_name=_("Points"),
        help_text=_("Points awarded for this answer"),
    )

    is_correct = models.BooleanField(
        null=True,
        blank=True,
        verbose_name=_("Is Correct"),
        help_text=_("Auto-graded or manually graded result"),
    )

    partial_credit = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
        verbose_name=_("Partial Credit"),
        help_text=_("0.0 to 1.0 for partial credit"),
    )

    feedback = models.TextField(
        blank=True,
        verbose_name=_("Feedback"),
        help_text=_("Instructor feedback for this answer"),
    )

    # Timestamps
    answered_at = models.DateTimeField(auto_now=True, verbose_name=_("Answered At"))

    graded_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Graded At"))

    graded_by = models.ForeignKey(
        Profile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="graded_quiz_answers",
        verbose_name=_("Graded By"),
    )

    class Meta:
        verbose_name = _("Quiz Answer")
        verbose_name_plural = _("Quiz Answers")
        unique_together = ("attempt", "question")
        ordering = ["attempt", "question"]

    def __str__(self):
        return f"{self.attempt} - {self.question.title}"

    def auto_grade(self):
        """
        Auto-grade this answer based on the question's correct answers.

        Returns:
            True if the answer was auto-graded (even if incorrect)
            False if the answer cannot be auto-graded (e.g., essay)
        """
        if not self.question.correct_answers:
            return False

        result = None
        answer_text = self.answer

        if self.question.question_type in ["MC", "TF"]:
            # Single choice: Check if selected choice ID matches the correct one
            correct_id = self.question.correct_answers.get("answers")
            if correct_id:
                result = answer_text == correct_id

        elif self.question.question_type == "MA":
            # Multiple Answer: Check if selected set matches correct set exactly
            correct_ids = self.question.correct_answers.get("answers", [])
            if isinstance(answer_text, str):
                try:
                    answer_text = json.loads(answer_text)
                except:
                    result = False
            if (
                result is None
                and isinstance(correct_ids, list)
                and isinstance(answer_text, list)
            ):
                result = set(answer_text) == set(correct_ids)

        elif self.question.question_type == "SA":
            import re

            # Short Answer: Check against exact matches or regex patterns
            ca_type = self.question.correct_answers.get("type", "exact")
            answers = self.question.correct_answers.get("answers", [])
            case_sensitive = self.question.correct_answers.get("case_sensitive", False)

            if ca_type == "exact":
                # Exact match checking
                if case_sensitive:
                    result = answer_text in answers
                else:
                    result = answer_text.lower() in [a.lower() for a in answers]
            elif ca_type == "regex":
                # Regex pattern matching
                flags = 0 if case_sensitive else re.IGNORECASE
                result = False
                for pattern in answers:
                    try:
                        if re.match(pattern, answer_text, flags):
                            result = True
                            break
                    except:
                        continue

        # Essay questions need manual grading
        if result is not None:
            self.is_correct = result
            self.partial_credit = 1.0 if result else 0.0

            # Calculate points based on assignment
            try:
                assignment = QuizQuestionAssignment.objects.get(
                    quiz=self.attempt.quiz, question=self.question
                )
                self.points = assignment.points * float(self.partial_credit)
            except QuizQuestionAssignment.DoesNotExist:
                self.points = 0

            self.graded_at = timezone.now()
            self.save(
                update_fields=["is_correct", "partial_credit", "points", "graded_at"]
            )
            return True

        return False

    def get_formatted_answer(self):
        """Get human-readable version of the answer"""
        if self.question.question_type in ["MC", "TF"]:
            # For single choice, look up the text from choices
            if (
                self.question.choices
                and isinstance(self.question.choices, list)
                and self.answer
            ):
                for choice in self.question.choices:
                    if choice["id"] == self.answer:
                        return choice["text"]

        elif self.question.question_type == "MA":
            # For multiple answer, look up texts for all selected choices
            if (
                self.question.choices
                and isinstance(self.question.choices, list)
                and self.answer
            ):
                try:
                    selected_ids = (
                        json.loads(self.answer)
                        if isinstance(self.answer, str)
                        else self.answer
                    )
                    selected_texts = []
                    for choice in self.question.choices:
                        if choice["id"] in selected_ids:
                            selected_texts.append(choice["text"])
                    return ", ".join(selected_texts)
                except:
                    pass

        return self.answer


class QuizAnswerFile(models.Model):
    """File attachments for essay answers"""

    answer = models.ForeignKey(
        QuizAnswer, on_delete=models.CASCADE, related_name="files"
    )

    file = models.FileField(upload_to=quiz_answer_file_path, verbose_name=_("File"))

    original_filename = models.CharField(
        max_length=255, verbose_name=_("Original Filename")
    )

    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name=_("Uploaded At"))

    class Meta:
        verbose_name = _("Quiz Answer File")
        verbose_name_plural = _("Quiz Answer Files")
        ordering = ["uploaded_at"]

    def __str__(self):
        return f"{self.answer} - {self.original_filename}"

    def save(self, *args, **kwargs):
        if self.file and not self.original_filename:
            self.original_filename = self.file.name
        super().save(*args, **kwargs)

    def get_file_size(self):
        """Return file size in human-readable format"""
        try:
            size = self.file.size
        except (OSError, ValueError):
            return "Unknown"

        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def get_file_extension(self):
        """Return file extension"""
        if self.original_filename:
            parts = self.original_filename.rsplit(".", 1)
            if len(parts) > 1:
                return parts[1].lower()
        return ""
