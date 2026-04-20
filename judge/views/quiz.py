import csv
import json
import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, Max, Avg
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseRedirect,
    JsonResponse,
)
from django.shortcuts import get_object_or_404, render, redirect
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import (
    ListView,
    View,
    DetailView,
    CreateView,
    UpdateView,
    DeleteView,
)

from judge.tasks.llm import (
    improve_question_markdown_task,
    generate_question_explanation_task,
)

from judge.widgets import HeavySelect2MultipleWidget, HeavyPreviewPageDownWidget

from judge.models import (
    Quiz,
    QuizQuestion,
    QuizQuestionAssignment,
    QuizAttempt,
    QuizAnswer,
    QuizAnswerFile,
    CourseLessonQuiz,
    CourseLesson,
    Profile,
    ContestProblem,
)
from judge.models.quiz import QuizQuestionType
from judge.models.course import Course
from judge.utils.views import (
    TitleMixin,
    DiggPaginatorMixin,
    paginate_query_context,
)

# =============================================================================
# Permission Mixins
# =============================================================================


class QuizEditorMixin(UserPassesTestMixin):
    """Mixin that checks if user can create/manage quizzes.

    Permission is granted to any authenticated user.
    Object-level editing is protected by QuizObjectEditorMixin.
    """

    def test_func(self):
        return self.request.user.is_authenticated

    def handle_no_permission(self):
        raise PermissionDenied(_("You do not have permission to manage quizzes."))


class QuestionAccessMixin(UserPassesTestMixin):
    """Mixin that checks if user can view a specific question.

    Access is granted if:
    - Question is public
    - User is an author or curator of the question
    - User is superuser
    """

    def test_func(self):
        if not self.request.user.is_authenticated:
            return False
        question = self.get_object()
        # Public questions are accessible by all authenticated users
        if question.is_public:
            return True
        if self.request.user.is_superuser:
            return True
        # Check if user is author or curator
        return question.is_editor(self.request.user.profile)

    def handle_no_permission(self):
        raise Http404()


class QuestionEditorMixin(UserPassesTestMixin):
    """Mixin that checks if user can edit a specific question."""

    def test_func(self):
        if not self.request.user.is_authenticated:
            return False
        if self.request.user.is_superuser:
            return True
        # Get the question object
        question = self.get_object()
        return question.is_editable_by(self.request.user)

    def handle_no_permission(self):
        raise PermissionDenied(_("You do not have permission to edit this question."))


class QuizObjectEditorMixin(UserPassesTestMixin):
    """Mixin that checks if user can edit a specific quiz."""

    def test_func(self):
        if not self.request.user.is_authenticated:
            return False
        if self.request.user.is_superuser:
            return True
        quiz = self.get_object()
        return quiz.is_editable_by(self.request.user)

    def handle_no_permission(self):
        raise PermissionDenied(_("You do not have permission to edit this quiz."))


class PendingGradingCountMixin:
    """Mixin to add pending grading count to context for the sidebar badge.

    In contest mode: Shows only pending grading from quizzes in the current contest.
    """

    @property
    def _in_contest(self):
        """Check if user is currently in a contest"""
        return (
            self.request.user.is_authenticated
            and self.request.profile.current_contest is not None
            and self.request.in_contest
        )

    def get_pending_grading_count(self):
        """Get count of attempts that need grading."""
        if not self.request.user.is_authenticated:
            return 0

        pending_base = QuizAttempt.objects.filter(
            is_submitted=True,
            answers__question__question_type="ES",
            answers__graded_at__isnull=True,
        )

        # In contest mode, filter to attempts taken during this contest
        if self._in_contest:
            participation = self.request.profile.current_contest
            # Filter by contest_participation to only count attempts taken during this contest
            pending_base = pending_base.filter(
                contest_participation__contest=participation.contest
            )

            # Still apply permission checks: only count for quizzes user can edit
            if not self.request.user.is_superuser:
                profile = getattr(self.request, "profile", None)
                if profile:
                    pending_base = pending_base.filter(
                        Q(quiz__authors=profile) | Q(quiz__curators=profile)
                    )
                else:
                    return 0
        elif not self.request.user.is_superuser:
            profile = getattr(self.request, "profile", None)
            if profile:
                pending_base = pending_base.filter(
                    Q(quiz__authors=profile) | Q(quiz__curators=profile)
                )
            else:
                return 0

        return pending_base.distinct().count()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pending_grading_count"] = self.get_pending_grading_count()
        return context


# =============================================================================
# Question Bank Views (Teachers/TAs only)
# =============================================================================


class QuestionBankList(
    LoginRequiredMixin,
    PendingGradingCountMixin,
    DiggPaginatorMixin,
    TitleMixin,
    ListView,
):
    """List all questions with filters (type, created_by, tags), search by name/tags.

    Access control similar to Problem list:
    - Public questions are visible to all authenticated users
    - Private questions are only visible to authors, curators, or superusers

    In contest mode: Shows only questions from quizzes in the current contest.
    """

    model = QuizQuestion
    template_name = "quiz/question_bank/list.html"
    context_object_name = "questions"
    title = gettext_lazy("Question Bank")
    paginate_by = 50

    @property
    def in_contest(self):
        """Check if user is currently in a contest"""
        return (
            self.request.user.is_authenticated
            and self.request.profile.current_contest is not None
            and self.request.in_contest
        )

    def get_queryset(self):
        queryset = QuizQuestion.objects.prefetch_related("authors__user")

        # In contest mode, show only questions from quizzes in the current contest
        if self.in_contest:
            participation = self.request.profile.current_contest
            # Get quiz IDs from the current contest
            contest_quiz_ids = participation.contest.contest_problems.filter(
                quiz__isnull=False
            ).values_list("quiz_id", flat=True)
            # Get question IDs from those quizzes
            contest_question_ids = QuizQuestionAssignment.objects.filter(
                quiz_id__in=contest_quiz_ids
            ).values_list("question_id", flat=True)
            queryset = queryset.filter(id__in=contest_question_ids)

            # Still apply permission checks: public questions OR user's own questions
            if not self.request.user.is_superuser:
                profile = self.request.profile
                queryset = queryset.filter(
                    Q(is_public=True) | Q(authors=profile) | Q(curators=profile)
                ).distinct()
        else:
            # Access control: public questions OR user's own questions
            if not self.request.user.is_superuser:
                profile = self.request.profile
                queryset = queryset.filter(
                    Q(is_public=True) | Q(authors=profile) | Q(curators=profile)
                ).distinct()

        # Filter by search term
        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search)
                | Q(tags__icontains=search)
                | Q(content__icontains=search)
            )

        # Filter by question type
        question_type = self.request.GET.get("type", "")
        if question_type:
            queryset = queryset.filter(question_type=question_type)

        # Filter by author
        author = self.request.GET.get("author", "")
        if author:
            queryset = queryset.filter(authors__user__username=author)

        # Filter by public status (only applies if user can see both)
        is_public = self.request.GET.get("public", "")
        if is_public == "true":
            queryset = queryset.filter(is_public=True)
        elif is_public == "false":
            queryset = queryset.filter(is_public=False)

        return queryset.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search"] = self.request.GET.get("search", "")
        context["selected_type"] = self.request.GET.get("type", "")
        context["selected_author"] = self.request.GET.get("author", "")
        context["question_types"] = QuizQuestionType.choices
        context["page_type"] = "questions"
        context["in_contest"] = self.in_contest
        # Check if user can create questions (teacher/TA/admin)
        context["can_create"] = self._can_create_questions()
        if self.in_contest:
            context["current_contest"] = self.request.profile.current_contest.contest

        # Add pagination context for query parameter pagination
        context.update(paginate_query_context(self.request))

        return context

    def _can_create_questions(self):
        """Check if user can create new questions (any authenticated user)"""
        return self.request.user.is_authenticated


class QuizQuestionForm(forms.ModelForm):
    """Form for creating/editing quiz questions with markdown support."""

    class Meta:
        model = QuizQuestion
        fields = [
            "title",
            "question_type",
            "content",
            "choices",
            "correct_answers",
            "grading_strategy",
            "shuffle_choices",
            "tags",
            "is_public",
            "explanation",
        ]
        widgets = {
            "content": HeavyPreviewPageDownWidget(
                preview=reverse_lazy("blog_preview"),
                attrs={"style": "max-width: 100%"},
            ),
            "explanation": HeavyPreviewPageDownWidget(
                preview=reverse_lazy("blog_preview"),
                attrs={"style": "max-width: 100%"},
            ),
        }


logger = logging.getLogger(__name__)


class QuizAIMixin:
    """Mixin that adds AI features (markdown improvement, explanation generation) to quiz views.
    Superuser-only. Intercepts AJAX POSTs before normal form processing."""

    def handle_improve_question_markdown(self, request):
        """Handle AI markdown improvement request — dispatches async Celery task"""
        try:
            if not request.user.is_superuser:
                return JsonResponse({"success": False, "error": "Permission denied"})

            content = request.POST.get("content", "").strip()
            if not content:
                return JsonResponse({"success": False, "error": "No content provided"})

            choices_json = request.POST.get("choices", "").strip()

            task = improve_question_markdown_task.delay(content, choices_json)

            return JsonResponse(
                {
                    "success": True,
                    "task_id": task.id,
                    "status": "processing",
                }
            )

        except Exception as e:
            logger.error(f"Error in quiz AI markdown improvement: {e}")
            return JsonResponse(
                {"success": False, "error": "An unexpected error occurred"}
            )

    def handle_generate_explanation(self, request):
        """Handle AI explanation generation/improvement — dispatches async Celery task"""
        try:
            if not request.user.is_superuser:
                return JsonResponse({"success": False, "error": "Permission denied"})

            question_content = request.POST.get("content", "").strip()
            if not question_content:
                return JsonResponse(
                    {"success": False, "error": "No question content provided"}
                )

            question_type = request.POST.get("question_type", "MC")
            choices_json = request.POST.get("choices", "")
            correct_answers_json = request.POST.get("correct_answers", "")
            existing_explanation = request.POST.get("explanation", "").strip()
            rough_ideas = request.POST.get("rough_ideas", "").strip()

            task = generate_question_explanation_task.delay(
                question_content=question_content,
                question_type=question_type,
                choices_json=choices_json,
                correct_answers_json=correct_answers_json,
                existing_explanation=existing_explanation,
                rough_ideas=rough_ideas,
            )

            return JsonResponse(
                {
                    "success": True,
                    "task_id": task.id,
                    "status": "processing",
                }
            )

        except Exception as e:
            logger.error(f"Error in quiz AI explanation generation: {e}")
            return JsonResponse(
                {"success": False, "error": "An unexpected error occurred"}
            )


class QuestionBankCreate(
    LoginRequiredMixin,
    QuizEditorMixin,
    QuizAIMixin,
    PendingGradingCountMixin,
    TitleMixin,
    CreateView,
):
    """Create new question + choices. Any authenticated user can create questions."""

    model = QuizQuestion
    template_name = "quiz/question_bank/create.html"
    title = gettext_lazy("Create Question")
    form_class = QuizQuestionForm

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not self.request.user.is_superuser:
            form.fields["is_public"].disabled = True
        return form

    def get_success_url(self):
        return reverse("question_bank_detail", args=[self.object.pk])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "questions"
        return context

    def post(self, request, *args, **kwargs):
        if request.POST.get("improve_question_markdown") == "1":
            return self.handle_improve_question_markdown(request)
        if request.POST.get("generate_explanation") == "1":
            return self.handle_generate_explanation(request)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save(commit=False)
            if not self.request.user.is_superuser:
                self.object.is_public = False
            self.object.save()
            form.save_m2m()
            # Add creator as author
            self.object.authors.add(self.request.profile)
        messages.success(self.request, _("Question created successfully."))
        return HttpResponseRedirect(self.get_success_url())


class QuestionBankEdit(
    LoginRequiredMixin,
    QuestionEditorMixin,
    QuizAIMixin,
    PendingGradingCountMixin,
    TitleMixin,
    UpdateView,
):
    """Edit question + choices."""

    model = QuizQuestion
    template_name = "quiz/question_bank/edit.html"
    form_class = QuizQuestionForm

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        self._original_is_public = obj.is_public
        return obj

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not self.request.user.is_superuser:
            # If currently private, disable (can't set public without admin)
            # If currently public, allow unchecking (public -> private is ok)
            if not self._original_is_public:
                form.fields["is_public"].disabled = True
        return form

    def get_title(self):
        return _("Edit Question: %s") % self.object.title

    def get_success_url(self):
        return reverse("question_bank_detail", args=[self.object.pk])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "questions"
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if request.POST.get("improve_question_markdown") == "1":
            return self.handle_improve_question_markdown(request)
        if request.POST.get("generate_explanation") == "1":
            return self.handle_generate_explanation(request)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        if not self.request.user.is_superuser:
            # Non-superusers cannot set private -> public
            if not self._original_is_public:
                form.instance.is_public = False
        messages.success(self.request, _("Question updated successfully."))
        return super().form_valid(form)


class QuestionBankDetail(
    LoginRequiredMixin,
    QuestionAccessMixin,
    PendingGradingCountMixin,
    TitleMixin,
    DetailView,
):
    """View question details (read-only preview).

    Access control similar to Problem:
    - Public questions can be viewed by any authenticated user
    - Private questions can only be viewed by authors, curators, or superusers
    """

    model = QuizQuestion
    template_name = "quiz/question_bank/detail.html"
    context_object_name = "question"

    def get_title(self):
        return self.object.title

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = self.object.is_editable_by(self.request.user)
        # Check which quizzes use this question
        context["used_in_quizzes"] = Quiz.objects.filter(
            quiz_questions__question=self.object
        ).distinct()
        context["page_type"] = "questions"
        return context


class QuestionBankDelete(
    LoginRequiredMixin, QuestionEditorMixin, TitleMixin, DeleteView
):
    """Delete question (confirm modal, check if used in quizzes)."""

    model = QuizQuestion
    template_name = "quiz/question_bank/delete.html"

    def get_title(self):
        return _("Delete Question: %s") % self.object.title

    def get_success_url(self):
        return reverse("question_bank_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Check if used in quizzes
        context["used_in_quizzes"] = Quiz.objects.filter(
            quiz_questions__question=self.object
        ).distinct()
        return context

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        # Check if used in quizzes
        used_count = Quiz.objects.filter(quiz_questions__question=self.object).count()
        if used_count > 0:
            messages.error(
                request,
                _("Cannot delete question. It is used in %d quiz(es).") % used_count,
            )
            return redirect("question_bank_detail", pk=self.object.pk)

        messages.success(request, _("Question deleted successfully."))
        return super().delete(request, *args, **kwargs)


# =============================================================================
# Quiz Management Views (Teachers/TAs only)
# =============================================================================


class QuizList(
    PendingGradingCountMixin,
    DiggPaginatorMixin,
    TitleMixin,
    ListView,
):
    """List all quizzes.

    In contest mode: Shows only quizzes from the current contest (accessible to all participants).
    Outside contest mode: Shows public quizzes + quizzes the user can edit.
    Anonymous users can see public quizzes.
    """

    model = Quiz
    template_name = "quiz/list.html"
    context_object_name = "quizzes"
    title = gettext_lazy("Quizzes")
    paginate_by = 50

    @property
    def in_contest(self):
        """Check if user is currently in a contest"""
        return (
            self.request.user.is_authenticated
            and self.request.profile.current_contest is not None
            and self.request.in_contest
        )

    def get_queryset(self):
        # In contest mode, show only quizzes from the current contest
        if self.in_contest:
            participation = self.request.profile.current_contest
            contest_quiz_ids = participation.contest.contest_problems.filter(
                quiz__isnull=False
            ).values_list("quiz_id", flat=True)
            queryset = Quiz.objects.filter(id__in=contest_quiz_ids)
        else:
            # Outside contest mode, show public quizzes and quizzes the user can edit
            queryset = Quiz.objects.prefetch_related("authors__user")

            if not self.request.user.is_authenticated:
                # Anonymous users can only see public quizzes
                queryset = queryset.filter(is_public=True)
            elif not self.request.user.is_superuser:
                # Non-superusers see public quizzes + own quizzes + quizzes they can test
                profile = self.request.profile
                queryset = queryset.filter(
                    Q(is_public=True)
                    | Q(authors=profile)
                    | Q(curators=profile)
                    | Q(testers=profile)
                ).distinct()

        # Filter by search term
        search = self.request.GET.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(code__icontains=search) | Q(title__icontains=search)
            )

        return queryset.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["search"] = self.request.GET.get("search", "")
        context["in_contest"] = self.in_contest

        # Add best scores and attempt counts for authenticated users
        if self.request.user.is_authenticated:
            profile = self.request.profile
            quizzes = context.get("quizzes", [])
            quiz_ids = [q.id for q in quizzes]

            # Get best scores for each quiz
            best_scores = {}
            attempt_counts = {}
            quiz_points = {}

            for quiz_id in quiz_ids:
                # Filter attempts by context
                attempt_filter = {
                    "user": profile,
                    "quiz_id": quiz_id,
                    "is_submitted": True,
                }
                if self.in_contest:
                    attempt_filter["contest_participation"] = profile.current_contest
                else:
                    # Standalone: only standalone attempts
                    attempt_filter["lesson_quiz__isnull"] = True
                    attempt_filter["contest_participation__isnull"] = True

                best_attempt = (
                    QuizAttempt.objects.filter(**attempt_filter)
                    .order_by("-score")
                    .first()
                )
                if best_attempt and best_attempt.score is not None:
                    best_scores[quiz_id] = float(best_attempt.score)

                attempt_counts[quiz_id] = QuizAttempt.objects.filter(
                    **attempt_filter
                ).count()

            # Get quiz points from contest if in contest mode
            if self.in_contest:
                participation = self.request.profile.current_contest
                for cp in participation.contest.contest_problems.filter(
                    quiz__isnull=False
                ):
                    quiz_points[cp.quiz_id] = cp.points
                context["current_contest"] = participation.contest

            context["best_scores"] = best_scores
            context["attempt_counts"] = attempt_counts
            context["quiz_points"] = quiz_points
        else:
            context["best_scores"] = {}
            context["attempt_counts"] = {}
            context["quiz_points"] = {}

        context["page_type"] = "list"

        # Add pagination context for query parameter pagination
        context.update(paginate_query_context(self.request))

        return context


class QuizCreateForm(forms.ModelForm):
    """Form for creating quiz with proper widgets for ManyToMany fields."""

    class Meta:
        model = Quiz
        fields = [
            "code",
            "title",
            "description",
            "time_limit",
            "shuffle_questions",
            "is_shown_correctness",
            "is_shown_answer",
            "is_public",
            "authors",
            "curators",
            "testers",
        ]
        widgets = {
            "authors": HeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "curators": HeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "testers": HeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "description": HeavyPreviewPageDownWidget(
                preview=reverse_lazy("blog_preview"),
                attrs={"style": "max-width: 100%"},
            ),
        }


class QuizCreate(
    LoginRequiredMixin,
    QuizEditorMixin,
    PendingGradingCountMixin,
    TitleMixin,
    CreateView,
):
    """Create quiz (similar to contest create UI). Any authenticated user can create quizzes."""

    model = Quiz
    template_name = "quiz/create.html"
    title = gettext_lazy("Create Quiz")
    form_class = QuizCreateForm

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not self.request.user.is_superuser:
            form.fields["is_public"].disabled = True
        return form

    def get_success_url(self):
        return reverse("quiz_edit", args=[self.object.code])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "create"
        return context

    def form_valid(self, form):
        with transaction.atomic():
            self.object = form.save(commit=False)
            if not self.request.user.is_superuser:
                self.object.is_public = False
            self.object.save()
            form.save_m2m()
            # Add creator as author
            self.object.authors.add(self.request.profile)

            # Process question data from the form
            question_data = self.request.POST.get("question_data")
            if question_data:
                try:
                    questions = json.loads(question_data)
                    for order, q_data in enumerate(questions):
                        question_id = q_data.get("questionId")
                        points = max(0, min(1000, int(q_data.get("points", 1))))
                        if question_id:
                            try:
                                qs = QuizQuestion.objects.filter(pk=question_id)
                                if not self.request.user.is_superuser:
                                    profile = self.request.profile
                                    qs = qs.filter(
                                        Q(is_public=True)
                                        | Q(authors=profile)
                                        | Q(curators=profile)
                                    )
                                question = qs.get()
                                QuizQuestionAssignment.objects.create(
                                    quiz=self.object,
                                    question=question,
                                    points=points,
                                    order=order,
                                )
                            except QuizQuestion.DoesNotExist:
                                pass
                except (json.JSONDecodeError, KeyError):
                    pass

        if self.object.quiz_questions.exists():
            messages.success(
                self.request, _("Quiz created successfully with questions.")
            )
        else:
            messages.success(
                self.request, _("Quiz created successfully. Now add questions.")
            )
        return HttpResponseRedirect(self.get_success_url())


class QuizEditForm(forms.ModelForm):
    """Form for editing quiz with proper widgets for ManyToMany fields."""

    class Meta:
        model = Quiz
        fields = [
            "code",
            "title",
            "description",
            "time_limit",
            "shuffle_questions",
            "is_shown_correctness",
            "is_shown_answer",
            "is_public",
            "authors",
            "curators",
            "testers",
        ]
        widgets = {
            "authors": HeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "curators": HeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "testers": HeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "description": HeavyPreviewPageDownWidget(
                preview=reverse_lazy("blog_preview"),
                attrs={"style": "max-width: 100%"},
            ),
        }


class QuizEdit(
    LoginRequiredMixin,
    QuizObjectEditorMixin,
    PendingGradingCountMixin,
    TitleMixin,
    UpdateView,
):
    """Edit quiz settings + assign questions."""

    model = Quiz
    template_name = "quiz/edit.html"
    slug_field = "code"
    slug_url_kwarg = "code"
    form_class = QuizEditForm

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        self._original_is_public = obj.is_public
        return obj

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not self.request.user.is_superuser:
            # If currently private, disable (can't set public without admin)
            # If currently public, allow unchecking (public -> private is ok)
            if not self._original_is_public:
                form.fields["is_public"].disabled = True
        return form

    def get_title(self):
        return _("Edit Quiz: %s") % self.object.title

    def get_success_url(self):
        return reverse("quiz_edit", args=[self.object.code])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_type"] = "edit"
        context["quiz"] = self.object
        context["can_edit"] = True
        # Get assigned questions
        context["assigned_questions"] = (
            QuizQuestionAssignment.objects.filter(quiz=self.object)
            .select_related("question")
            .order_by("order")
        )
        # Get available questions for assignment
        context["available_questions"] = (
            QuizQuestion.objects.filter(
                Q(is_public=True)
                | Q(authors=self.request.profile)
                | Q(curators=self.request.profile)
            )
            .exclude(
                id__in=context["assigned_questions"].values_list(
                    "question_id", flat=True
                )
            )
            .distinct()[:100]
        )
        return context

    def form_valid(self, form):
        if not self.request.user.is_superuser:
            # Non-superusers cannot set private -> public
            if not self._original_is_public:
                form.instance.is_public = False
        response = super().form_valid(form)

        # Process question changes from the form
        question_changes = self.request.POST.get("question_changes")
        if question_changes:
            try:
                import json

                changes = json.loads(question_changes)
                self._apply_question_changes(changes)
            except (json.JSONDecodeError, KeyError):
                pass

        messages.success(self.request, _("Quiz updated successfully."))
        return response

    def _apply_question_changes(self, changes):
        """Apply question assignment changes (add, remove, update, reorder)."""
        quiz = self.object

        # 1. Remove questions marked for deletion
        removed_ids = changes.get("removed", [])
        if removed_ids:
            QuizQuestionAssignment.objects.filter(
                quiz=quiz, id__in=removed_ids
            ).delete()

        # 2. Update existing question points
        updated = changes.get("updated", {})
        for assignment_id, update_data in updated.items():
            try:
                assignment = QuizQuestionAssignment.objects.get(
                    quiz=quiz, id=int(assignment_id)
                )
                if "points" in update_data:
                    assignment.points = max(0, min(1000, int(update_data["points"])))
                    assignment.save()
            except (QuizQuestionAssignment.DoesNotExist, ValueError):
                pass

        # 3. Add new questions
        added = changes.get("added", [])
        current_max_order = (
            QuizQuestionAssignment.objects.filter(quiz=quiz).aggregate(
                max_order=Max("order")
            )["max_order"]
            or 0
        )

        for i, new_question in enumerate(added):
            question_id = new_question.get("questionId")
            points = max(0, min(1000, int(new_question.get("points", 1))))
            if question_id:
                try:
                    qs = QuizQuestion.objects.filter(pk=question_id)
                    if not self.request.user.is_superuser:
                        profile = self.request.profile
                        qs = qs.filter(
                            Q(is_public=True) | Q(authors=profile) | Q(curators=profile)
                        )
                    question = qs.get()
                    QuizQuestionAssignment.objects.get_or_create(
                        quiz=quiz,
                        question=question,
                        defaults={"points": points, "order": current_max_order + i + 1},
                    )
                except QuizQuestion.DoesNotExist:
                    pass

        # 4. Update order based on the order array
        order_list = changes.get("order", [])
        for index, assignment_id in enumerate(order_list):
            try:
                QuizQuestionAssignment.objects.filter(
                    quiz=quiz, id=int(assignment_id)
                ).update(order=index)
            except ValueError:
                pass


class QuizDelete(LoginRequiredMixin, QuizObjectEditorMixin, TitleMixin, DeleteView):
    """Delete quiz."""

    model = Quiz
    template_name = "quiz/delete.html"
    slug_field = "code"
    slug_url_kwarg = "code"

    def get_title(self):
        return _("Delete Quiz: %s") % self.object.title

    def get_success_url(self):
        return reverse("quiz_list")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Check for existing attempts
        context["attempt_count"] = QuizAttempt.objects.filter(quiz=self.object).count()
        return context

    def delete(self, request, *args, **kwargs):
        messages.success(request, _("Quiz deleted successfully."))
        return super().delete(request, *args, **kwargs)


class QuizRegrade(LoginRequiredMixin, QuizObjectEditorMixin, View):
    """Regrade all attempts for a quiz."""

    def get_object(self):
        return get_object_or_404(Quiz, code=self.kwargs["code"])

    def post(self, request, *args, **kwargs):
        quiz = self.get_object()
        count = quiz.regrade_all_attempts()
        messages.success(
            request, _("Successfully regraded %(count)d attempts.") % {"count": count}
        )
        return redirect("quiz_edit", code=quiz.code)


class QuizAddQuestion(LoginRequiredMixin, QuizObjectEditorMixin, View):
    """AJAX endpoint to add a question to a quiz."""

    def get_object(self):
        return get_object_or_404(Quiz, code=self.kwargs["code"])

    def post(self, request, *args, **kwargs):
        quiz = self.get_object()
        question_id = request.POST.get("question_id")
        points = max(0, min(1000, int(request.POST.get("points", 1))))

        try:
            qs = QuizQuestion.objects.filter(pk=question_id)
            if not request.user.is_superuser:
                profile = request.profile
                qs = qs.filter(
                    Q(is_public=True) | Q(authors=profile) | Q(curators=profile)
                )
            question = qs.get()
        except QuizQuestion.DoesNotExist:
            return JsonResponse({"error": "Question not found"}, status=404)

        # Get next order number
        max_order = (
            QuizQuestionAssignment.objects.filter(quiz=quiz).aggregate(
                max=Max("order")
            )["max"]
            or 0
        )

        assignment, created = QuizQuestionAssignment.objects.get_or_create(
            quiz=quiz,
            question=question,
            defaults={"points": points, "order": max_order + 1},
        )

        if not created:
            return JsonResponse({"error": "Question already in quiz"}, status=400)

        return JsonResponse(
            {
                "success": True,
                "assignment_id": assignment.id,
                "question_title": question.title,
            }
        )


class QuizRemoveQuestion(LoginRequiredMixin, QuizObjectEditorMixin, View):
    """AJAX endpoint to remove a question from a quiz."""

    def get_object(self):
        return get_object_or_404(Quiz, code=self.kwargs["code"])

    def post(self, request, *args, **kwargs):
        quiz = self.get_object()
        assignment_id = request.POST.get("assignment_id")

        try:
            assignment = QuizQuestionAssignment.objects.get(pk=assignment_id, quiz=quiz)
            assignment.delete()
            return JsonResponse({"success": True})
        except QuizQuestionAssignment.DoesNotExist:
            return JsonResponse({"error": "Assignment not found"}, status=404)


class QuizReorderQuestions(LoginRequiredMixin, QuizObjectEditorMixin, View):
    """AJAX endpoint to reorder questions in a quiz."""

    def get_object(self):
        return get_object_or_404(Quiz, code=self.kwargs["code"])

    def post(self, request, *args, **kwargs):
        quiz = self.get_object()

        try:
            order_data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        with transaction.atomic():
            for item in order_data:
                QuizQuestionAssignment.objects.filter(pk=item["id"], quiz=quiz).update(
                    order=item["order"]
                )

        return JsonResponse({"success": True})


class QuizUpdatePoints(LoginRequiredMixin, QuizObjectEditorMixin, View):
    """AJAX endpoint to update points for a question in a quiz."""

    def get_object(self):
        return get_object_or_404(Quiz, code=self.kwargs["code"])

    def post(self, request, *args, **kwargs):
        quiz = self.get_object()
        assignment_id = request.POST.get("assignment_id")
        points = request.POST.get("points")

        try:
            points = int(points)
            if points < 0 or points > 1000:
                return JsonResponse(
                    {"error": "Points must be between 0 and 1000"}, status=400
                )
        except (TypeError, ValueError):
            return JsonResponse({"error": "Invalid points value"}, status=400)

        try:
            assignment = QuizQuestionAssignment.objects.get(pk=assignment_id, quiz=quiz)
            assignment.points = points
            assignment.save(update_fields=["points"])
            return JsonResponse({"success": True, "points": points})
        except QuizQuestionAssignment.DoesNotExist:
            return JsonResponse({"error": "Assignment not found"}, status=404)


class QuizSearchQuestions(LoginRequiredMixin, QuizObjectEditorMixin, View):
    """AJAX endpoint to search questions for adding to quiz."""

    def get_object(self):
        return get_object_or_404(Quiz, code=self.kwargs["code"])

    def get(self, request, *args, **kwargs):
        quiz = self.get_object()
        query = request.GET.get("q", "").strip()

        # Get IDs of questions already assigned to this quiz
        assigned_ids = QuizQuestionAssignment.objects.filter(quiz=quiz).values_list(
            "question_id", flat=True
        )

        # Get questions that user can access and are not already in quiz
        questions = QuizQuestion.objects.filter(
            Q(is_public=True) | Q(authors=request.profile) | Q(curators=request.profile)
        ).exclude(id__in=assigned_ids)

        # Filter by search query
        if query:
            questions = questions.filter(
                Q(title__icontains=query) | Q(tags__icontains=query)
            )

        # Limit results and ensure distinct
        questions = questions.distinct()[:20]

        return JsonResponse(
            {
                "questions": [
                    {
                        "id": q.id,
                        "title": q.title,
                        "type": q.get_question_type_display(),
                    }
                    for q in questions
                ]
            }
        )


class QuizValidateQuestions(LoginRequiredMixin, QuizEditorMixin, View):
    """AJAX endpoint to validate question IDs for batch adding to a quiz.

    Accepts POST with JSON body {"ids": [1, 2, 3, ...]}.
    Returns JSON with valid questions and invalid IDs.
    """

    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
            ids = data.get("ids", [])
        except (json.JSONDecodeError, AttributeError):
            return JsonResponse({"error": _("Invalid request body.")}, status=400)

        if not isinstance(ids, list) or not ids:
            return JsonResponse({"error": _("No IDs provided.")}, status=400)

        # Sanitize: keep only valid integers
        clean_ids = []
        for item in ids:
            try:
                clean_ids.append(int(item))
            except (ValueError, TypeError):
                continue

        if not clean_ids:
            return JsonResponse({"error": _("No valid IDs provided.")}, status=400)

        # Query accessible questions with the same permission pattern as Select2
        queryset = QuizQuestion.objects.filter(pk__in=clean_ids)
        if not request.user.is_superuser:
            profile = request.user.profile
            queryset = queryset.filter(
                Q(is_public=True)
                | Q(authors__id=profile.id)
                | Q(curators__id=profile.id)
            ).distinct()

        valid_questions = [
            {
                "id": q.id,
                "title": q.title,
                "type": q.get_question_type_display(),
            }
            for q in queryset
        ]
        found_ids = {q["id"] for q in valid_questions}
        invalid_ids = [i for i in clean_ids if i not in found_ids]

        return JsonResponse(
            {
                "valid": valid_questions,
                "invalid_ids": invalid_ids,
            }
        )


# =============================================================================
# Course Lesson Quiz Views
# =============================================================================


class CourseLessonQuizCreate(
    LoginRequiredMixin, QuizEditorMixin, TitleMixin, CreateView
):
    """Attach quiz to lesson."""

    model = CourseLessonQuiz
    template_name = "quiz/course_lesson/create.html"
    title = gettext_lazy("Add Quiz to Lesson")
    fields = ["quiz", "max_attempts", "points", "order", "is_visible"]

    def dispatch(self, request, *args, **kwargs):
        self.lesson = self.get_lesson()
        course = self.lesson.course
        if not Course.is_editable_by(course, request.profile):
            raise PermissionDenied(_("You do not have permission to edit this course."))
        return super().dispatch(request, *args, **kwargs)

    def get_lesson(self):
        if not hasattr(self, "lesson"):
            self.lesson = get_object_or_404(CourseLesson, pk=self.kwargs["lesson_id"])
        return self.lesson

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["lesson"] = self.get_lesson()
        if self.request.user.is_superuser:
            context["available_quizzes"] = Quiz.objects.all()[:100]
        else:
            profile = self.request.profile
            context["available_quizzes"] = Quiz.objects.filter(
                Q(is_public=True) | Q(authors=profile) | Q(curators=profile)
            ).distinct()[:100]
        return context

    def form_valid(self, form):
        form.instance.lesson = self.get_lesson()
        messages.success(self.request, _("Quiz added to lesson successfully."))
        return super().form_valid(form)

    def get_success_url(self):
        lesson = self.get_lesson()
        return reverse("course_lesson_detail", args=[lesson.course.slug, lesson.id])


class CourseLessonQuizEdit(LoginRequiredMixin, QuizEditorMixin, TitleMixin, UpdateView):
    """Edit lesson quiz settings (attempts, visibility, points)."""

    model = CourseLessonQuiz
    template_name = "quiz/course_lesson/edit.html"
    pk_url_kwarg = "quiz_id"
    fields = ["max_attempts", "points", "order", "is_visible"]

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if not Course.is_editable_by(obj.lesson.course, request.profile):
            raise PermissionDenied(_("You do not have permission to edit this course."))
        return super().dispatch(request, *args, **kwargs)

    def get_title(self):
        return _("Edit Lesson Quiz Settings")

    def get_success_url(self):
        return reverse(
            "course_lesson_detail",
            args=[self.object.lesson.course.slug, self.object.lesson.id],
        )

    def form_valid(self, form):
        messages.success(self.request, _("Lesson quiz settings updated."))
        return super().form_valid(form)


class CourseLessonQuizDelete(
    LoginRequiredMixin, QuizEditorMixin, TitleMixin, DeleteView
):
    """Remove quiz from lesson."""

    model = CourseLessonQuiz
    template_name = "quiz/course_lesson/delete.html"
    pk_url_kwarg = "quiz_id"

    def dispatch(self, request, *args, **kwargs):
        obj = self.get_object()
        if not Course.is_editable_by(obj.lesson.course, request.profile):
            raise PermissionDenied(_("You do not have permission to edit this course."))
        return super().dispatch(request, *args, **kwargs)

    def get_title(self):
        return _("Remove Quiz from Lesson")

    def get_success_url(self):
        return reverse(
            "course_lesson_detail",
            args=[self.object.lesson.course.slug, self.object.lesson.id],
        )

    def delete(self, request, *args, **kwargs):
        messages.success(request, _("Quiz removed from lesson."))
        return super().delete(request, *args, **kwargs)


# =============================================================================
# Student Views
# =============================================================================


class QuizDetail(TitleMixin, DetailView):
    """View quiz info before starting (time limit, num questions, etc.)."""

    model = Quiz
    template_name = "quiz/detail.html"
    slug_field = "code"
    slug_url_kwarg = "code"
    context_object_name = "quiz"

    def get_object(self, queryset=None):
        quiz = super().get_object(queryset)
        in_contest = getattr(self.request, "in_contest", False)
        if not quiz.is_accessible_by(self.request.user, in_contest):
            raise Http404(_("Quiz not found."))
        return quiz

    def get_title(self):
        return self.object.title

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quiz = self.object

        context["can_edit"] = quiz.is_editable_by(self.request.user)
        context["question_count"] = quiz.get_question_count()
        context["total_points"] = quiz.get_total_points()

        # Get quiz questions for the table display
        context["quiz_questions"] = quiz.get_questions()

        # Provide standalone URLs for templates
        context["start_url"] = reverse("quiz_start", kwargs={"code": quiz.code})
        context["detail_url"] = reverse("quiz_detail", kwargs={"code": quiz.code})

        if self.request.user.is_authenticated:
            profile = self.request.profile
            context["user_attempts"] = QuizAttempt.objects.filter(
                user=profile, quiz=quiz
            ).order_by("-start_time")[:10]
            context["best_score"] = quiz.get_best_score(profile)
            context["attempts_count"] = QuizAttempt.objects.filter(
                user=profile, quiz=quiz, is_submitted=True
            ).count()
            context["in_progress_attempt"] = QuizAttempt.objects.filter(
                user=profile, quiz=quiz, is_submitted=False
            ).first()

            # Check if in contest mode and show max submissions info
            # Use request.in_contest to respect the "Out contest" toggle
            # Spectators should not see contest-specific limits
            if (
                getattr(self.request, "in_contest", False)
                and profile.current_contest
                and not profile.current_contest.spectate
            ):
                contest_quiz = ContestProblem.objects.filter(
                    contest=profile.current_contest.contest, quiz=quiz
                ).first()
                if contest_quiz and contest_quiz.max_submissions > 0:
                    contest_attempt_count = QuizAttempt.objects.filter(
                        user=profile,
                        quiz=quiz,
                        contest_participation=profile.current_contest,
                        is_submitted=True,
                    ).count()
                    context["submission_limit"] = contest_quiz.max_submissions
                    context["submissions_left"] = max(
                        contest_quiz.max_submissions - contest_attempt_count, 0
                    )

            # Lesson-specific max_attempts is now handled by LessonQuizDetail

        return context


class QuizStart(LoginRequiredMixin, View):
    """Start new attempt (create QuizAttempt, redirect to take page).

    Permission checks:
    1. Quiz must be accessible by user
    2. If in contest: contest must be active and not ended
    3. If lesson quiz: check max_attempts limit
    4. Multiple tab prevention via session
    """

    def post(self, request, *args, **kwargs):
        quiz = get_object_or_404(Quiz, code=kwargs["code"])

        in_contest = getattr(request, "in_contest", False)
        if not quiz.is_accessible_by(request.user, in_contest):
            raise Http404(_("Quiz not found."))

        profile = request.profile

        # Check if user is in a contest and this quiz is part of it
        contest_participation = None

        # Respect the "Out contest" toggle - only apply contest rules if in_contest is True
        # Spectators (virtual=-1) should not be bound by contest rules
        in_contest = getattr(request, "in_contest", False)
        if (
            in_contest
            and profile.current_contest
            and not profile.current_contest.spectate
        ):
            # Check if this quiz is in the current contest
            contest_quiz = ContestProblem.objects.filter(
                contest=profile.current_contest.contest, quiz=quiz
            ).first()
            if contest_quiz:
                contest_participation = profile.current_contest

                # Contest time validation: check if contest has started and not ended
                if not contest_participation.contest.can_join:
                    messages.error(request, _("Contest has not started yet."))
                    return redirect("quiz_detail", code=quiz.code)

                if contest_participation.ended:
                    messages.error(request, _("Contest has ended."))
                    return redirect("quiz_detail", code=quiz.code)

                # Check if participation time remaining
                if (
                    contest_participation.time_remaining is None
                    or contest_participation.time_remaining.total_seconds() <= 0
                ):
                    messages.error(request, _("Your contest time has ended."))
                    return redirect("quiz_detail", code=quiz.code)

                # Check max submissions limit for quiz in contest
                if contest_quiz.max_submissions > 0:
                    # Count submitted attempts for this quiz in this contest participation
                    attempt_count = QuizAttempt.objects.filter(
                        user=profile,
                        quiz=quiz,
                        contest_participation=contest_participation,
                        is_submitted=True,
                    ).count()
                    if attempt_count >= contest_quiz.max_submissions:
                        messages.error(
                            request,
                            _(
                                "You have reached the maximum number of attempts (%d) for this quiz."
                            )
                            % contest_quiz.max_submissions,
                        )
                        return redirect("quiz_detail", code=quiz.code)

        # Lesson quiz context is now handled by LessonQuizStart view

        # Check for in-progress attempt (for this specific context)
        existing_filter = {
            "user": profile,
            "quiz": quiz,
            "is_submitted": False,
        }
        # If in contest, only look for attempts in this contest
        if contest_participation:
            existing_filter["contest_participation"] = contest_participation
        else:
            existing_filter["contest_participation__isnull"] = True

        # Standalone: only look for attempts without lesson context
        existing_filter["lesson_quiz__isnull"] = True

        existing = QuizAttempt.objects.filter(**existing_filter).first()
        if existing:
            # Multiple tab prevention: store attempt ID in session
            request.session[f"quiz_attempt_{quiz.code}"] = existing.id
            return redirect("quiz_take", code=quiz.code, attempt_id=existing.id)

        # Determine time limit - use contest time remaining if in contest
        time_limit = quiz.time_limit
        if contest_participation:
            # If in contest, cap time limit to contest time remaining
            contest_time_remaining = contest_participation.time_remaining
            if contest_time_remaining:
                contest_minutes_remaining = int(
                    contest_time_remaining.total_seconds() / 60
                )
                if time_limit:
                    time_limit = min(time_limit, contest_minutes_remaining)
                else:
                    time_limit = contest_minutes_remaining

        # Create new standalone/contest attempt (no lesson context)
        attempt = QuizAttempt.objects.create(
            user=profile,
            quiz=quiz,
            attempt_number=QuizAttempt.objects.filter(user=profile, quiz=quiz).count()
            + 1,
            time_limit_minutes=time_limit,
            contest_participation=contest_participation,
        )

        # Multiple tab prevention: store attempt ID in session
        request.session[f"quiz_attempt_{quiz.code}"] = attempt.id

        return redirect("quiz_take", code=quiz.code, attempt_id=attempt.id)

    def get(self, request, *args, **kwargs):
        return redirect("quiz_detail", code=kwargs["code"])


class QuizTake(LoginRequiredMixin, TitleMixin, DetailView):
    """Take quiz interface (show questions, accept answers).

    Security features:
    - Server-side time enforcement
    - Multiple tab prevention via session
    - Contest time validation
    """

    model = QuizAttempt
    template_name = "quiz/take.html"
    context_object_name = "attempt"
    pk_url_kwarg = "attempt_id"

    def get_title(self):
        return _("Taking: %s") % self.object.quiz.title

    def get_object(self, queryset=None):
        attempt = get_object_or_404(
            QuizAttempt, pk=self.kwargs["attempt_id"], quiz__code=self.kwargs["code"]
        )

        if attempt.user != self.request.profile:
            raise PermissionDenied(_("This is not your attempt."))

        if attempt.is_submitted:
            # Redirect to results
            return redirect(
                "quiz_result", code=attempt.quiz.code, attempt_id=attempt.id
            )

        # Server-side time enforcement: check if expired and auto-submit
        if attempt.is_expired():
            attempt.auto_submit()
            messages.info(
                self.request,
                _("Time has expired. Your quiz has been submitted automatically."),
            )
            return redirect(
                "quiz_result", code=attempt.quiz.code, attempt_id=attempt.id
            )

        # Contest time enforcement
        if attempt.contest_participation:
            participation = attempt.contest_participation
            if participation.ended:
                attempt.auto_submit()
                messages.info(
                    self.request,
                    _("Contest has ended. Your quiz has been submitted automatically."),
                )
                return redirect(
                    "quiz_result", code=attempt.quiz.code, attempt_id=attempt.id
                )

            time_remaining = participation.time_remaining
            if time_remaining is None or time_remaining.total_seconds() <= 0:
                attempt.auto_submit()
                messages.info(
                    self.request,
                    _(
                        "Your contest time has ended. Your quiz has been submitted automatically."
                    ),
                )
                return redirect(
                    "quiz_result", code=attempt.quiz.code, attempt_id=attempt.id
                )

        return attempt

    def get(self, request, *args, **kwargs):
        result = self.get_object()
        if isinstance(result, HttpResponseRedirect):
            return result
        self.object = result

        # Multiple tab prevention: check session
        session_attempt_id = request.session.get(
            f"quiz_attempt_{self.object.quiz.code}"
        )
        if session_attempt_id and session_attempt_id != self.object.id:
            # User has a different attempt in session - warn them
            messages.warning(
                request,
                _(
                    "Warning: You may have this quiz open in another tab. Only one tab should be active."
                ),
            )

        # Update session with current attempt
        request.session[f"quiz_attempt_{self.object.quiz.code}"] = self.object.id

        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        quiz = attempt.quiz

        context["quiz"] = quiz
        # Get assignments (includes question and points)
        # Template expects 'questions' to be assignments (each has .question and .points)
        assignments = attempt.get_question_assignments()
        context["questions"] = assignments
        context["question_count"] = len(assignments)
        answers = QuizAnswer.objects.filter(attempt=attempt).prefetch_related("files")
        context["answers"] = {a.question_id: a for a in answers}
        context["time_remaining"] = attempt.time_remaining()
        context["has_time_limit"] = quiz.time_limit is not None and quiz.time_limit > 0

        # Provide standalone URLs for templates
        context["save_url"] = reverse(
            "quiz_save_answer",
            kwargs={"code": quiz.code, "attempt_id": attempt.id},
        )
        context["upload_url"] = reverse(
            "quiz_upload_file",
            kwargs={"code": quiz.code, "attempt_id": attempt.id},
        )
        context["submit_url"] = reverse(
            "quiz_submit",
            kwargs={"code": quiz.code, "attempt_id": attempt.id},
        )
        context["result_url"] = reverse(
            "quiz_result",
            kwargs={"code": quiz.code, "attempt_id": attempt.id},
        )

        # Build uploaded files dict for essay questions: {question_id: [file_info, ...]}
        uploaded_files = {}
        for answer in answers:
            if answer.files.exists():
                uploaded_files[answer.question_id] = [
                    {
                        "id": f.id,
                        "filename": f.original_filename,
                        "size": f.get_file_size(),
                        "url": f.file.url if f.file else None,
                        "extension": f.get_file_extension(),
                    }
                    for f in answer.files.all()
                ]
        context["uploaded_files"] = uploaded_files

        return context


class QuizSaveAnswer(LoginRequiredMixin, View):
    """AJAX endpoint to save a single answer.

    Server-side time enforcement:
    - Rejects answers after quiz time limit expires
    - Rejects answers after contest time ends
    """

    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        attempt_id = kwargs.get("attempt_id")
        question_id = data.get("question_id")
        answer = data.get("answer", "")

        try:
            attempt = QuizAttempt.objects.select_related("contest_participation").get(
                id=attempt_id, user=request.profile, is_submitted=False
            )
        except QuizAttempt.DoesNotExist:
            return JsonResponse({"error": "Attempt not found"}, status=404)

        # Server-side time enforcement: quiz time limit
        if attempt.is_expired():
            attempt.auto_submit()
            return JsonResponse({"error": "Time expired", "expired": True}, status=400)

        # Server-side time enforcement: contest time
        if attempt.contest_participation:
            participation = attempt.contest_participation
            if participation.ended:
                attempt.auto_submit()
                return JsonResponse(
                    {"error": "Contest ended", "expired": True}, status=400
                )
            time_remaining = participation.time_remaining
            if time_remaining is None or time_remaining.total_seconds() <= 0:
                attempt.auto_submit()
                return JsonResponse(
                    {"error": "Contest time ended", "expired": True}, status=400
                )

        try:
            question = QuizQuestion.objects.get(id=question_id)
            if not QuizQuestionAssignment.objects.filter(
                quiz=attempt.quiz, question=question
            ).exists():
                return JsonResponse({"error": "Invalid question"}, status=400)
        except QuizQuestion.DoesNotExist:
            return JsonResponse({"error": "Question not found"}, status=404)

        quiz_answer, created = QuizAnswer.objects.update_or_create(
            attempt=attempt,
            question=question,
            defaults={"answer": answer, "answered_at": timezone.now()},
        )

        return JsonResponse(
            {
                "success": True,
                "answer_id": quiz_answer.id,
                "saved_at": timezone.now().isoformat(),
            }
        )


class QuizSubmit(LoginRequiredMixin, View):
    """Submit quiz (auto-grade, save answers).

    Server-side time enforcement:
    - Auto-submits if time has expired (still processes answers)
    - Clears session on submission
    """

    def post(self, request, *args, **kwargs):
        attempt = get_object_or_404(
            QuizAttempt.objects.select_related("contest_participation"),
            pk=kwargs["attempt_id"],
            quiz__code=kwargs["code"],
            user=request.profile,
        )

        if attempt.is_submitted:
            messages.warning(request, _("This attempt was already submitted."))
            return redirect(
                "quiz_result", code=attempt.quiz.code, attempt_id=attempt.id
            )

        # Check if time has expired - still process but note it
        time_expired = attempt.is_expired()
        contest_expired = False
        if attempt.contest_participation:
            participation = attempt.contest_participation
            if participation.ended:
                contest_expired = True
            else:
                time_remaining = participation.time_remaining
                if time_remaining is None or time_remaining.total_seconds() <= 0:
                    contest_expired = True

        with transaction.atomic():
            # Get all questions in this quiz
            quiz_questions = attempt.quiz.quiz_questions.select_related("question")
            answered_question_ids = set()

            # Process form data and create/update answers
            for key, value in request.POST.items():
                if key.startswith("q_"):
                    try:
                        question_id = int(key[2:])  # Remove "q_" prefix
                        question = QuizQuestion.objects.get(pk=question_id)
                        # Verify question belongs to this quiz
                        if not QuizQuestionAssignment.objects.filter(
                            quiz=attempt.quiz, question=question
                        ).exists():
                            continue
                        answered_question_ids.add(question_id)

                        # For checkboxes (multiple answer), collect all values
                        if question.question_type == "MA":
                            values = request.POST.getlist(key)
                            answer_text = json.dumps(values)
                        else:
                            answer_text = value

                        # Create or update the answer
                        answer, created = QuizAnswer.objects.update_or_create(
                            attempt=attempt,
                            question=question,
                            defaults={"answer": answer_text},
                        )
                    except (ValueError, QuizQuestion.DoesNotExist):
                        continue

            # Create empty answers for unanswered questions
            for assignment in quiz_questions:
                if assignment.question_id not in answered_question_ids:
                    QuizAnswer.objects.get_or_create(
                        attempt=attempt,
                        question=assignment.question,
                        defaults={"answer": ""},
                    )

            attempt.end_time = timezone.now()
            attempt.is_submitted = True
            attempt.save(update_fields=["end_time", "is_submitted"])

            # Auto-grade all answers using utility function
            from judge.utils.quiz_grading import (
                auto_grade_quiz_attempt,
                notify_graders_for_essay,
            )

            auto_grade_quiz_attempt(attempt)

            # Notify graders if there are essay questions that need grading
            notify_graders_for_essay(attempt)

        # Clear session attempt tracking
        session_key = f"quiz_attempt_{attempt.quiz.code}"
        if session_key in request.session:
            del request.session[session_key]

        if time_expired or contest_expired:
            messages.info(
                request,
                _(
                    "Time had expired. Your answers were saved and the quiz has been submitted."
                ),
            )
        else:
            messages.success(request, _("Quiz submitted successfully!"))
        return redirect("quiz_result", code=attempt.quiz.code, attempt_id=attempt.id)

    def get(self, request, *args, **kwargs):
        return redirect(
            "quiz_take", code=kwargs["code"], attempt_id=kwargs["attempt_id"]
        )


# =============================================================================
# File Upload Security Constants
# =============================================================================

# Allowed file extensions for essay question attachments
ALLOWED_FILE_EXTENSIONS = {
    # Documents
    "pdf",
    "doc",
    "docx",
    "txt",
    "rtf",
    "odt",
    # Images
    "jpg",
    "jpeg",
    "png",
    "gif",
    "bmp",
    "webp",
    # Archives (for code submissions)
    "zip",
}

# Maximum file size in bytes (10 MB)
MAX_FILE_SIZE = 10 * 1024 * 1024


class QuizUploadFile(LoginRequiredMixin, View):
    """AJAX endpoint to upload file attachments for essay questions.

    Security features:
    - Whitelist allowed file extensions
    - Validate file size
    - Randomized filenames (via quiz_answer_file_path)
    - Server-side time enforcement
    - Only allows uploads for user's own attempts
    """

    def post(self, request, *args, **kwargs):
        attempt_id = kwargs.get("attempt_id")
        question_id = request.POST.get("question_id")
        uploaded_file = request.FILES.get("file")

        if not uploaded_file:
            return JsonResponse({"error": "No file provided"}, status=400)

        # Validate file extension (whitelist)
        original_filename = uploaded_file.name
        file_ext = (
            original_filename.rsplit(".", 1)[-1].lower()
            if "." in original_filename
            else ""
        )
        if file_ext not in ALLOWED_FILE_EXTENSIONS:
            return JsonResponse(
                {
                    "error": f"File type '.{file_ext}' is not allowed. Allowed types: {', '.join(sorted(ALLOWED_FILE_EXTENSIONS))}"
                },
                status=400,
            )

        # Validate file size
        if uploaded_file.size > MAX_FILE_SIZE:
            max_mb = MAX_FILE_SIZE / (1024 * 1024)
            return JsonResponse(
                {"error": f"File size exceeds maximum allowed ({max_mb:.0f} MB)"},
                status=400,
            )

        # Get the attempt
        try:
            attempt = QuizAttempt.objects.select_related("contest_participation").get(
                id=attempt_id, user=request.profile, is_submitted=False
            )
        except QuizAttempt.DoesNotExist:
            return JsonResponse({"error": "Attempt not found"}, status=404)

        # Server-side time enforcement
        if attempt.is_expired():
            attempt.auto_submit()
            return JsonResponse({"error": "Time expired", "expired": True}, status=400)

        if attempt.contest_participation:
            participation = attempt.contest_participation
            if participation.ended:
                attempt.auto_submit()
                return JsonResponse(
                    {"error": "Contest ended", "expired": True}, status=400
                )
            time_remaining = participation.time_remaining
            if time_remaining is None or time_remaining.total_seconds() <= 0:
                attempt.auto_submit()
                return JsonResponse(
                    {"error": "Contest time ended", "expired": True}, status=400
                )

        # Validate question exists and is essay type
        try:
            question = QuizQuestion.objects.get(id=question_id)
            if question.question_type != "ES":
                return JsonResponse(
                    {"error": "File uploads only allowed for essay questions"},
                    status=400,
                )
            if not QuizQuestionAssignment.objects.filter(
                quiz=attempt.quiz, question=question
            ).exists():
                return JsonResponse({"error": "Invalid question"}, status=400)
        except QuizQuestion.DoesNotExist:
            return JsonResponse({"error": "Question not found"}, status=404)

        # Get or create the answer
        answer, created = QuizAnswer.objects.get_or_create(
            attempt=attempt, question=question, defaults={"answer": ""}
        )

        # Create the file attachment (filename is randomized in quiz_answer_file_path)
        file_obj = QuizAnswerFile.objects.create(
            answer=answer,
            file=uploaded_file,
            original_filename=original_filename,
        )

        return JsonResponse(
            {
                "success": True,
                "file_id": file_obj.id,
                "filename": file_obj.original_filename,
                "size": file_obj.get_file_size(),
                "url": file_obj.file.url if file_obj.file else None,
                "extension": file_obj.get_file_extension(),
            }
        )


class QuizDeleteFile(LoginRequiredMixin, View):
    """AJAX endpoint to delete a file attachment."""

    def post(self, request, *args, **kwargs):
        file_id = kwargs.get("file_id")

        try:
            file_obj = QuizAnswerFile.objects.select_related("answer__attempt").get(
                id=file_id
            )
        except QuizAnswerFile.DoesNotExist:
            return JsonResponse({"error": "File not found"}, status=404)

        # Check ownership
        if file_obj.answer.attempt.user != request.profile:
            return JsonResponse({"error": "Permission denied"}, status=403)

        # Check if attempt is still in progress
        if file_obj.answer.attempt.is_submitted:
            return JsonResponse(
                {"error": "Cannot delete files from submitted attempts"}, status=400
            )

        # Delete the file
        file_obj.file.delete(save=False)
        file_obj.delete()

        return JsonResponse({"success": True})


class QuizResult(LoginRequiredMixin, TitleMixin, DetailView):
    """View results after submission (score, correct answers if enabled)."""

    model = QuizAttempt
    template_name = "quiz/result.html"
    context_object_name = "attempt"
    pk_url_kwarg = "attempt_id"

    def get_title(self):
        return _("Results: %s") % self.object.quiz.title

    def get_object(self, queryset=None):
        attempt = get_object_or_404(
            QuizAttempt.objects.select_related("quiz", "user"),
            pk=self.kwargs["attempt_id"],
            quiz__code=self.kwargs["code"],
        )

        quiz = attempt.quiz

        # Check quiz accessibility (respects contest mode toggle)
        in_contest = getattr(self.request, "in_contest", False)
        if not quiz.is_accessible_by(self.request.user, in_contest):
            raise Http404(_("Quiz not found."))

        is_owner = attempt.user == self.request.profile
        # Cache is_editor to avoid repeated DB queries in get_context_data
        self._is_editor = quiz.is_editable_by(self.request.user)
        if not (is_owner or self._is_editor):
            raise PermissionDenied(_("You cannot view this result."))

        return attempt

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        quiz = attempt.quiz
        is_editor = self._is_editor

        answers = list(
            QuizAnswer.objects.filter(attempt=attempt)
            .select_related("question")
            .prefetch_related("files")
            .order_by("question__id")
        )

        # Batch-load max points to avoid N+1 queries from get_max_points()
        question_ids = [a.question_id for a in answers]
        assignments = QuizQuestionAssignment.objects.filter(
            quiz=quiz, question_id__in=question_ids
        )
        max_points_map = {a.question_id: a.points for a in assignments}
        for answer in answers:
            answer._max_points = max_points_map.get(answer.question_id, 1.0)

        context["answers"] = answers
        context["show_answers"] = is_editor or quiz.is_shown_answer
        context["show_correctness"] = is_editor or quiz.is_shown_correctness
        context["can_edit"] = is_editor

        # Check if results should be hidden in contest context
        is_result_hidden = False
        if attempt.contest_participation_id:
            cp = ContestProblem.objects.filter(
                contest=attempt.contest_participation.contest, quiz=quiz
            ).first()
            if cp:
                is_result_hidden = quiz.should_hide_result(
                    self.request.user, contest_problem=cp
                )
        context["is_result_hidden"] = is_result_hidden

        # Check if user can retake the quiz
        context["can_retake"] = (
            attempt.user == self.request.profile
            and not QuizAttempt.objects.filter(
                quiz=quiz, user=self.request.profile, is_submitted=False
            ).exists()
        )

        # Provide standalone URLs for templates
        context["detail_url"] = reverse("quiz_detail", kwargs={"code": quiz.code})
        context["start_url"] = reverse("quiz_start", kwargs={"code": quiz.code})

        if attempt.max_score and attempt.max_score > 0:
            context["percentage"] = round(
                (attempt.score or 0) / attempt.max_score * 100, 1
            )
        else:
            context["percentage"] = 0

        return context


class QuizAttemptList(LoginRequiredMixin, TitleMixin, ListView):
    """View all attempts for a quiz (student's own or another user's if editor)."""

    model = QuizAttempt
    template_name = "quiz/attempt_list.html"
    context_object_name = "attempts"
    paginate_by = 20

    def get_quiz(self):
        if not hasattr(self, "_quiz"):
            self._quiz = get_object_or_404(Quiz, code=self.kwargs["code"])
        return self._quiz

    def dispatch(self, request, *args, **kwargs):
        quiz = self.get_quiz()
        in_contest = getattr(request, "in_contest", False)
        if not quiz.is_accessible_by(request.user, in_contest):
            raise Http404(_("Quiz not found."))
        return super().dispatch(request, *args, **kwargs)

    def get_target_profile(self):
        """Get the profile whose attempts to show. Editors can view other users' attempts."""
        if not hasattr(self, "_target_profile"):
            username = self.request.GET.get("user")
            if username and username != self.request.profile.user.username:
                # Check if current user can view other users' attempts
                quiz = self.get_quiz()
                if quiz.is_editable_by(self.request.user):
                    from judge.models import Profile

                    try:
                        self._target_profile = Profile.objects.get(
                            user__username=username
                        )
                    except Profile.DoesNotExist:
                        self._target_profile = self.request.profile
                else:
                    self._target_profile = self.request.profile
            else:
                self._target_profile = self.request.profile
        return self._target_profile

    def get_title(self):
        target_profile = self.get_target_profile()
        if target_profile != self.request.profile:
            return _("Attempts by %(user)s: %(quiz)s") % {
                "user": target_profile.user.username,
                "quiz": self.get_quiz().title,
            }
        return _("My Attempts: %s") % self.get_quiz().title

    def get_queryset(self):
        quiz = self.get_quiz()
        profile = self.get_target_profile()
        queryset = QuizAttempt.objects.filter(quiz=quiz, user=profile)

        # Each context shows only its own attempts:
        # - Contest mode: contest attempts only
        # - Standalone: standalone attempts only (no lesson, no contest)
        in_contest = getattr(self.request, "in_contest", False)
        if (
            in_contest
            and profile.current_contest
            and not profile.current_contest.spectate
        ):
            contest_quiz = ContestProblem.objects.filter(
                contest=profile.current_contest.contest, quiz=quiz
            ).first()
            if contest_quiz:
                queryset = queryset.filter(
                    contest_participation=profile.current_contest
                )
        else:
            # Standalone: only attempts without lesson or contest context
            queryset = queryset.filter(
                lesson_quiz__isnull=True,
                contest_participation__isnull=True,
            )

        return queryset.order_by("-start_time")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quiz = self.get_quiz()
        target_profile = self.get_target_profile()
        context["quiz"] = quiz
        context["can_edit"] = quiz.is_editable_by(self.request.user)
        context["total_points"] = quiz.get_total_points()
        context["target_profile"] = target_profile
        context["viewing_other_user"] = target_profile != self.request.profile

        # Calculate best score based on filtered attempts
        attempts = self.object_list.filter(is_submitted=True)
        best_attempt = attempts.order_by("-score").first()
        context["best_score"] = best_attempt.score if best_attempt else None

        # Show context info (respect "Out contest" toggle)
        contest_quiz = None
        in_contest = getattr(self.request, "in_contest", False)
        if in_contest and self.request.profile.current_contest:
            contest_quiz = ContestProblem.objects.filter(
                contest=self.request.profile.current_contest.contest, quiz=quiz
            ).first()
            if contest_quiz:
                context["in_contest"] = True
                context["contest"] = self.request.profile.current_contest.contest

        # Check if results should be hidden
        is_result_hidden = False
        if contest_quiz:
            is_result_hidden = quiz.should_hide_result(
                self.request.user, contest_problem=contest_quiz
            )
        context["is_result_hidden"] = is_result_hidden

        # Add pagination context for query parameter pagination
        context.update(paginate_query_context(self.request))

        return context


# =============================================================================
# Grading Views (Teachers/TAs only)
# =============================================================================


class GradingDashboard(
    LoginRequiredMixin, QuizEditorMixin, PendingGradingCountMixin, TitleMixin, ListView
):
    """List all submitted attempts for grading.

    In contest mode: Shows only attempts from quizzes in the current contest.
    """

    model = QuizAttempt
    template_name = "quiz/grading/dashboard.html"
    context_object_name = "attempts"
    title = gettext_lazy("Grading Dashboard")
    paginate_by = 50

    @property
    def in_contest(self):
        """Check if user is currently in a contest"""
        return (
            self.request.user.is_authenticated
            and self.request.profile.current_contest is not None
            and self.request.in_contest
        )

    def get_queryset(self):
        # Get all submitted attempts
        queryset = (
            QuizAttempt.objects.filter(
                is_submitted=True,
            )
            .select_related("quiz", "user__user")
            .order_by("-end_time")
        )

        # In contest mode, filter to attempts taken during this contest
        if self.in_contest:
            participation = self.request.profile.current_contest
            # Filter by contest_participation to only show attempts taken during this contest
            queryset = queryset.filter(
                contest_participation__contest=participation.contest
            )

            # Still apply permission checks: only show attempts for quizzes user can edit
            if not self.request.user.is_superuser:
                profile = self.request.profile
                queryset = queryset.filter(
                    Q(quiz__authors=profile) | Q(quiz__curators=profile)
                ).distinct()
        elif not self.request.user.is_superuser:
            # If not superuser, only show attempts for quizzes user can edit
            profile = self.request.profile
            queryset = queryset.filter(
                Q(quiz__authors=profile) | Q(quiz__curators=profile)
            ).distinct()

        # Filter by quiz if specified
        quiz_code = self.request.GET.get("quiz", "")
        if quiz_code:
            queryset = queryset.filter(quiz__code=quiz_code)

        # Filter by user if specified (accepts both username and user ID)
        user_param = self.request.GET.get("user", "")
        if user_param:
            if user_param.isdigit():
                queryset = queryset.filter(user__id=int(user_param))
            else:
                queryset = queryset.filter(user__user__username=user_param)

        # Filter by status if specified
        status = self.request.GET.get("status", "")
        if status == "needs_grading":
            # Has essay questions that haven't been graded
            queryset = queryset.filter(
                answers__question__question_type="ES",
                answers__graded_at__isnull=True,
            ).distinct()
        elif status == "graded":
            # All essay questions have been graded (or no essay questions)
            queryset = queryset.exclude(
                answers__question__question_type="ES",
                answers__graded_at__isnull=True,
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["in_contest"] = self.in_contest

        # Get list of quizzes for filter dropdown
        if self.in_contest:
            # In contest mode, only show quizzes from the current contest that user can edit
            participation = self.request.profile.current_contest
            contest_quiz_ids = participation.contest.contest_problems.filter(
                quiz__isnull=False
            ).values_list("quiz_id", flat=True)
            quizzes = Quiz.objects.filter(id__in=contest_quiz_ids)
            # Still apply permission checks
            if not self.request.user.is_superuser:
                profile = self.request.profile
                quizzes = quizzes.filter(
                    Q(authors=profile) | Q(curators=profile)
                ).distinct()
            context["quizzes"] = quizzes
            context["current_contest"] = participation.contest
        elif self.request.user.is_superuser:
            context["quizzes"] = Quiz.objects.all()
        else:
            profile = self.request.profile
            context["quizzes"] = Quiz.objects.filter(
                Q(authors=profile) | Q(curators=profile)
            ).distinct()
        context["selected_quiz"] = self.request.GET.get("quiz", "")
        context["selected_status"] = self.request.GET.get("status", "")

        # Get selected user info (handle both ID and username)
        user_param = self.request.GET.get("user", "")
        context["selected_user"] = ""
        context["selected_user_id"] = ""
        if user_param:
            from judge.models import Profile

            try:
                if user_param.isdigit():
                    profile = Profile.objects.select_related("user").get(
                        id=int(user_param)
                    )
                else:
                    profile = Profile.objects.select_related("user").get(
                        user__username=user_param
                    )
                context["selected_user"] = profile.user.username
                context["selected_user_id"] = profile.id
            except Profile.DoesNotExist:
                pass

        # Add has_ungraded_essays flag to each attempt
        for attempt in context["attempts"]:
            attempt.has_ungraded_essays = attempt.answers.filter(
                question__question_type="ES",
                graded_at__isnull=True,
            ).exists()

        context["page_type"] = "grading"

        # Add pagination context for query parameter pagination
        context.update(paginate_query_context(self.request))

        return context


class AttemptGrade(LoginRequiredMixin, QuizEditorMixin, TitleMixin, DetailView):
    """Grade individual attempt (view all answers, assign points, feedback)."""

    model = QuizAttempt
    template_name = "quiz/grading/grade.html"
    context_object_name = "attempt"
    pk_url_kwarg = "attempt_id"

    def get_title(self):
        return _("Grade Attempt")

    def get_object(self, queryset=None):
        attempt = get_object_or_404(QuizAttempt, pk=self.kwargs["attempt_id"])
        if not attempt.quiz.is_editable_by(self.request.user):
            raise PermissionDenied(_("You cannot grade this quiz."))
        return attempt

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["answers"] = (
            QuizAnswer.objects.filter(attempt=self.object)
            .select_related("question")
            .prefetch_related("files")
            .order_by("question__id")
        )
        return context

    def post(self, request, *args, **kwargs):
        attempt = self.get_object()

        # Collect answer IDs that need updating
        answers_to_update = {}

        for key, value in request.POST.items():
            if key.startswith("points_"):
                answer_id = key.split("_")[1]
                if answer_id not in answers_to_update:
                    answers_to_update[answer_id] = {}
                answers_to_update[answer_id]["points"] = value
            elif key.startswith("feedback_"):
                answer_id = key.split("_")[1]
                if answer_id not in answers_to_update:
                    answers_to_update[answer_id] = {}
                answers_to_update[answer_id]["feedback"] = value
            elif key.startswith("partial_"):
                answer_id = key.split("_")[1]
                if answer_id not in answers_to_update:
                    answers_to_update[answer_id] = {}
                answers_to_update[answer_id]["partial_credit"] = value

        # Update each answer
        for answer_id, data in answers_to_update.items():
            try:
                answer = QuizAnswer.objects.get(id=answer_id, attempt=attempt)
                if "points" in data:
                    points = float(data["points"])
                    answer.points = points
                    answer.is_correct = points > 0
                if "feedback" in data:
                    answer.feedback = data["feedback"]
                if "partial_credit" in data:
                    # Convert from 0-100 percentage to 0.0-1.0 decimal
                    partial_value = float(data["partial_credit"]) / 100.0
                    answer.partial_credit = min(max(partial_value, 0.0), 1.0)
                answer.graded_at = timezone.now()
                answer.graded_by = request.profile
                answer.save()
            except (QuizAnswer.DoesNotExist, ValueError):
                continue

        attempt.calculate_score()
        messages.success(request, _("Grading saved successfully."))
        return redirect("grading_dashboard")


class AnswerGrade(LoginRequiredMixin, QuizEditorMixin, View):
    """Grade single answer (AJAX endpoint)."""

    def post(self, request, *args, **kwargs):
        answer_id = kwargs.get("answer_id")

        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON"}, status=400)

        try:
            answer = QuizAnswer.objects.select_related("attempt__quiz").get(
                pk=answer_id
            )
        except QuizAnswer.DoesNotExist:
            return JsonResponse({"error": "Answer not found"}, status=404)

        if not answer.attempt.quiz.is_editable_by(request.user):
            return JsonResponse({"error": "Permission denied"}, status=403)

        try:
            points = float(data.get("points", 0))

            answer.points = points
            answer.is_correct = points > 0
            answer.graded_at = timezone.now()
            answer.graded_by = request.profile
            answer.save()

            # Recalculate attempt score
            answer.attempt.calculate_score()

            return JsonResponse(
                {
                    "success": True,
                    "points": answer.points,
                    "graded_at": answer.graded_at.isoformat(),
                }
            )
        except ValueError:
            return JsonResponse({"error": "Invalid points value"}, status=400)


# =============================================================================
# Statistics Views
# =============================================================================


class QuizStatistics(LoginRequiredMixin, QuizObjectEditorMixin, TitleMixin, DetailView):
    """View quiz statistics (score distribution, averages, etc.)."""

    model = Quiz
    template_name = "quiz/statistics.html"
    context_object_name = "quiz"
    slug_field = "code"
    slug_url_kwarg = "code"

    def get_title(self):
        return _("Statistics: %s") % self.object.title

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quiz = self.object

        # Get submitted attempts
        attempts = QuizAttempt.objects.filter(quiz=quiz, is_submitted=True)

        # Basic stats
        context["total_attempts"] = attempts.count()
        context["unique_users"] = attempts.values("user").distinct().count()

        # Score stats
        score_stats = attempts.aggregate(
            avg_score=Avg("score"),
            max_score=Max("score"),
        )
        context["avg_score"] = score_stats["avg_score"]
        context["max_score"] = score_stats["max_score"]

        # Calculate standard deviation manually if there are attempts
        if context["total_attempts"] > 1 and context["avg_score"]:
            scores = list(attempts.values_list("score", flat=True))
            avg = float(context["avg_score"])
            variance = sum((float(s) - avg) ** 2 for s in scores if s) / len(scores)
            context["std_score"] = variance**0.5
        else:
            context["std_score"] = None

        # Score distribution (0-10%, 10-20%, ..., 90-100%)
        max_quiz_score = quiz.get_total_points()
        if max_quiz_score > 0 and context["total_attempts"] > 0:
            distribution = []
            for i in range(10):
                lower = i * 10
                upper = (i + 1) * 10
                lower_score = max_quiz_score * lower / 100
                upper_score = max_quiz_score * upper / 100

                count = attempts.filter(
                    score__gte=lower_score,
                    score__lt=upper_score if i < 9 else upper_score + 1,  # Include 100%
                ).count()

                distribution.append(
                    {
                        "range": f"{lower}-{upper}%",
                        "count": count,
                    }
                )
            context["distribution"] = distribution
        else:
            context["distribution"] = []

        # For tabs
        context["can_edit"] = True  # Already checked by QuizObjectEditorMixin

        return context


# =============================================================================
# Export Views
# =============================================================================


class QuizGradesExportCSV(LoginRequiredMixin, QuizObjectEditorMixin, View):
    """Export quiz grades to CSV file."""

    def get(self, request, *args, **kwargs):
        quiz = get_object_or_404(Quiz, code=kwargs["code"])

        if not quiz.is_editable_by(request.user):
            raise PermissionDenied(_("You cannot export this quiz's grades."))

        # Create the HttpResponse object with CSV header
        # Use UTF-8 with BOM for Excel compatibility with Vietnamese characters
        response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
        response["Content-Disposition"] = (
            f'attachment; filename="{quiz.code}_grades.csv"'
        )

        # Write UTF-8 BOM for Excel to recognize encoding
        response.write("\ufeff")

        writer = csv.writer(response)

        # Get all questions in order
        assignments = QuizQuestionAssignment.objects.filter(quiz=quiz).order_by(
            "order", "id"
        )
        questions = [a.question for a in assignments]

        # Build header row
        header = [
            "Username",
            "Email",
            "Attempt #",
            "Start Time",
            "End Time",
            "Score",
            "Max Score",
            "Percentage",
        ]
        for i, q in enumerate(questions, 1):
            header.append(f"Q{i}: {q.title[:30]}")
            header.append(f"Q{i} Points")
        writer.writerow(header)

        # Get all submitted attempts
        attempts = (
            QuizAttempt.objects.filter(quiz=quiz, is_submitted=True)
            .select_related("user__user")
            .prefetch_related("answers__question")
            .order_by("user__user__username", "attempt_number")
        )

        for attempt in attempts:
            # Build answer lookup for this attempt
            answer_lookup = {a.question_id: a for a in attempt.answers.all()}

            row = [
                attempt.user.user.username,
                attempt.user.user.email,
                attempt.attempt_number,
                (
                    attempt.start_time.strftime("%Y-%m-%d %H:%M:%S")
                    if attempt.start_time
                    else ""
                ),
                (
                    attempt.end_time.strftime("%Y-%m-%d %H:%M:%S")
                    if attempt.end_time
                    else ""
                ),
                attempt.score or 0,
                attempt.max_score or 0,
                (
                    f"{(attempt.score / attempt.max_score * 100):.1f}%"
                    if attempt.max_score
                    else "0%"
                ),
            ]

            # Add answer data for each question
            for q in questions:
                answer = answer_lookup.get(q.id)
                if answer:
                    # Truncate long answers for CSV
                    answer_text = answer.answer or ""
                    if len(answer_text) > 100:
                        answer_text = answer_text[:100] + "..."
                    row.append(answer_text)
                    row.append(answer.points or 0)
                else:
                    row.append("")
                    row.append(0)

            writer.writerow(row)

        return response


class QuizQuestionAnalysis(
    LoginRequiredMixin, QuizObjectEditorMixin, TitleMixin, DetailView
):
    """View per-question statistics and difficulty analysis."""

    model = Quiz
    template_name = "quiz/question_analysis.html"
    context_object_name = "quiz"
    slug_field = "code"
    slug_url_kwarg = "code"

    def get_title(self):
        return _("Question Analysis: %s") % self.object.title

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quiz = self.object

        # Get all question assignments in order
        assignments = (
            QuizQuestionAssignment.objects.filter(quiz=quiz)
            .select_related("question")
            .order_by("order", "id")
        )

        # Get all answers for submitted attempts
        question_stats = []

        for assignment in assignments:
            question = assignment.question
            max_points = assignment.points

            # Get all answers for this question from submitted attempts
            answers = QuizAnswer.objects.filter(
                question=question,
                attempt__quiz=quiz,
                attempt__is_submitted=True,
            )

            total_answers = answers.count()
            correct_answers = answers.filter(is_correct=True).count()
            total_points = sum(a.points or 0 for a in answers)

            # Calculate stats
            if total_answers > 0:
                correct_rate = (correct_answers / total_answers) * 100
                avg_points = total_points / total_answers
                avg_percentage = (avg_points / max_points * 100) if max_points else 0
            else:
                correct_rate = 0
                avg_points = 0
                avg_percentage = 0

            # Determine difficulty based on correct rate
            if correct_rate >= 80:
                difficulty = "Easy"
                difficulty_class = "success"
            elif correct_rate >= 50:
                difficulty = "Medium"
                difficulty_class = "warning"
            else:
                difficulty = "Hard"
                difficulty_class = "danger"

            # For MC/MA/TF, get choice distribution
            choice_distribution = None
            if question.question_type in ["MC", "MA", "TF"] and question.choices:
                choice_counts = {}
                for choice in question.choices:
                    choice_counts[choice["id"]] = {
                        "text": choice["text"],
                        "count": 0,
                    }

                for answer in answers:
                    if question.question_type == "MA":
                        try:
                            selected = (
                                json.loads(answer.answer) if answer.answer else []
                            )
                        except json.JSONDecodeError:
                            selected = []
                        for choice_id in selected:
                            if choice_id in choice_counts:
                                choice_counts[choice_id]["count"] += 1
                    else:
                        if answer.answer in choice_counts:
                            choice_counts[answer.answer]["count"] += 1

                # Calculate percentages
                choice_distribution = []
                for choice_id, data in choice_counts.items():
                    percentage = (
                        (data["count"] / total_answers * 100) if total_answers else 0
                    )
                    is_correct = False
                    if question.correct_answers:
                        correct = question.correct_answers.get("answers", "")
                        if isinstance(correct, list):
                            is_correct = choice_id in correct
                        else:
                            is_correct = choice_id == correct

                    choice_distribution.append(
                        {
                            "id": choice_id,
                            "text": data["text"],
                            "count": data["count"],
                            "percentage": percentage,
                            "is_correct": is_correct,
                        }
                    )

            question_stats.append(
                {
                    "question": question,
                    "order": assignment.order,
                    "max_points": max_points,
                    "total_answers": total_answers,
                    "correct_answers": correct_answers,
                    "correct_rate": correct_rate,
                    "avg_points": avg_points,
                    "avg_percentage": avg_percentage,
                    "difficulty": difficulty,
                    "difficulty_class": difficulty_class,
                    "choice_distribution": choice_distribution,
                }
            )

        context["question_stats"] = question_stats
        context["total_questions"] = len(question_stats)

        # For tabs
        context["can_edit"] = True  # Already checked by QuizObjectEditorMixin

        return context


class ContestQuizAttemptsAjax(View):
    """AJAX view to show quiz attempts for a user in a contest (for ranking popup)."""

    def get(self, request, contest, participation_id, quiz_id):
        from judge.models import Contest, ContestParticipation

        contest_obj = get_object_or_404(Contest, key=contest)
        participation = get_object_or_404(
            ContestParticipation, id=participation_id, contest=contest_obj
        )
        quiz = get_object_or_404(Quiz, id=quiz_id)

        # Get all submitted attempts for this quiz in this contest participation
        attempts = QuizAttempt.objects.filter(
            user=participation.user,
            quiz=quiz,
            contest_participation=participation,
            is_submitted=True,
        ).order_by("-end_time")

        # Calculate best score
        best_attempt = attempts.order_by("-score").first()

        # Check if results should be hidden
        cp = ContestProblem.objects.filter(contest=contest_obj, quiz=quiz).first()
        is_result_hidden = False
        if cp and cp.is_result_hidden:
            is_result_hidden = not contest_obj.is_editable_by(request.user)

        return render(
            request,
            "quiz/contest-attempts-ajax.html",
            {
                "contest": contest_obj,
                "participation": participation,
                "profile": participation.user,
                "quiz": quiz,
                "attempts": attempts,
                "best_attempt": best_attempt,
                "is_result_hidden": is_result_hidden,
            },
        )


class QuizGradingTab(
    LoginRequiredMixin,
    QuizObjectEditorMixin,
    PendingGradingCountMixin,
    TitleMixin,
    ListView,
):
    """Grade attempts for a specific quiz (filtered dashboard view).

    Only visible to quiz authors/curators on the quiz detail page.
    Uses quiz sidebar for navigation.
    """

    model = QuizAttempt
    template_name = "quiz/grading/quiz_grade.html"
    context_object_name = "attempts"
    paginate_by = 50

    def get_object(self):
        """Get the quiz from URL - required by QuizObjectEditorMixin."""
        if not hasattr(self, "_quiz"):
            self._quiz = get_object_or_404(Quiz, code=self.kwargs["code"])
        return self._quiz

    def get_title(self):
        return _("Grade: %s") % self.get_object().title

    def get_queryset(self):
        quiz = self.get_object()

        # Get all submitted attempts for this quiz
        queryset = (
            QuizAttempt.objects.filter(
                quiz=quiz,
                is_submitted=True,
            )
            .select_related("quiz", "user__user")
            .order_by("-end_time")
        )

        # Filter by user if specified (accepts both username and user ID)
        user_param = self.request.GET.get("user", "")
        if user_param:
            if user_param.isdigit():
                queryset = queryset.filter(user__id=int(user_param))
            else:
                queryset = queryset.filter(user__user__username=user_param)

        # Filter by status if specified
        status = self.request.GET.get("status", "")
        if status == "needs_grading":
            # Has essay questions that haven't been graded
            queryset = queryset.filter(
                answers__question__question_type="ES",
                answers__graded_at__isnull=True,
            ).distinct()
        elif status == "graded":
            # All essay questions have been graded (or no essay questions)
            queryset = queryset.exclude(
                answers__question__question_type="ES",
                answers__graded_at__isnull=True,
            )

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quiz = self.get_object()

        context["quiz"] = quiz
        context["can_edit"] = True  # Already verified by mixin

        # Selected filter values
        context["selected_status"] = self.request.GET.get("status", "")

        # Get selected user info (handle both ID and username)
        user_param = self.request.GET.get("user", "")
        context["selected_user"] = ""
        context["selected_user_id"] = ""
        if user_param:
            try:
                if user_param.isdigit():
                    profile = Profile.objects.select_related("user").get(
                        id=int(user_param)
                    )
                else:
                    profile = Profile.objects.select_related("user").get(
                        user__username=user_param
                    )
                context["selected_user"] = profile.user.username
                context["selected_user_id"] = profile.id
            except Profile.DoesNotExist:
                pass

        # Add has_ungraded_essays flag to each attempt
        for attempt in context["attempts"]:
            attempt.has_ungraded_essays = attempt.answers.filter(
                question__question_type="ES",
                graded_at__isnull=True,
            ).exists()

        context["page_type"] = "grade"

        # Add pagination context for query parameter pagination
        context.update(paginate_query_context(self.request))

        return context


# =============================================================================
# Lesson-Scoped Quiz Views
# =============================================================================


class LessonQuizMixin:
    """Mixin for lesson-scoped quiz views.

    Resolves course, lesson, and lesson_quiz from URL kwargs.
    Validates user enrollment in the course.
    Provides lesson context to templates and URL helpers.
    """

    def get_course(self):
        if not hasattr(self, "_course"):
            self._course = get_object_or_404(Course, slug=self.kwargs["slug"])
        return self._course

    def get_lesson(self):
        if not hasattr(self, "_lesson"):
            self._lesson = get_object_or_404(
                CourseLesson,
                id=self.kwargs["lesson_id"],
                course=self.get_course(),
            )
        return self._lesson

    def get_lesson_quiz(self):
        if not hasattr(self, "_lesson_quiz"):
            self._lesson_quiz = get_object_or_404(
                CourseLessonQuiz,
                lesson=self.get_lesson(),
                quiz__code=self.kwargs["code"],
                is_visible=True,
            )
        return self._lesson_quiz

    def check_enrollment(self, user):
        if not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return Course.is_accessible_by(self.get_course(), user.profile)

    def dispatch(self, request, *args, **kwargs):
        if not self.check_enrollment(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_lesson_url_kwargs(self, **extra):
        return {
            "slug": self.get_course().slug,
            "lesson_id": self.get_lesson().id,
            "code": self.get_lesson_quiz().quiz.code,
            **extra,
        }

    def get_lesson_quiz_urls(self, attempt_id=None):
        """Build URL dict for templates to use in both standalone and lesson contexts."""
        kw = self.get_lesson_url_kwargs()
        urls = {
            "detail_url": reverse("lesson_quiz_detail", kwargs=kw),
            "start_url": reverse("lesson_quiz_start", kwargs=kw),
            "attempts_url": reverse("lesson_quiz_attempt_list", kwargs=kw),
            "back_url": reverse(
                "course_lesson_detail",
                args=(self.get_course().slug, self.get_lesson().id),
            ),
        }
        if attempt_id:
            akw = {**kw, "attempt_id": attempt_id}
            urls.update(
                {
                    "take_url": reverse("lesson_quiz_take", kwargs=akw),
                    "save_url": reverse("lesson_quiz_save_answer", kwargs=akw),
                    "upload_url": reverse("lesson_quiz_upload_file", kwargs=akw),
                    "submit_url": reverse("lesson_quiz_submit", kwargs=akw),
                    "result_url": reverse("lesson_quiz_result", kwargs=akw),
                }
            )
        return urls

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["course"] = self.get_course()
        context["lesson"] = self.get_lesson()
        context["lesson_quiz"] = self.get_lesson_quiz()
        context["in_lesson_context"] = True
        return context

    def validate_attempt_context(self, attempt):
        """Validate that the attempt belongs to this lesson context."""
        lesson_quiz = self.get_lesson_quiz()
        if attempt.lesson_quiz_id != lesson_quiz.id:
            raise Http404(_("This attempt does not belong to this lesson."))


class LessonQuizDetail(LessonQuizMixin, TitleMixin, DetailView):
    """Quiz detail page in lesson context."""

    model = Quiz
    template_name = "quiz/detail.html"
    slug_field = "code"
    slug_url_kwarg = "code"
    context_object_name = "quiz"

    def get_object(self, queryset=None):
        return self.get_lesson_quiz().quiz

    def get_title(self):
        return self.object.title

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        quiz = self.object
        lesson_quiz = self.get_lesson_quiz()

        context["can_edit"] = quiz.is_editable_by(self.request.user)
        context["question_count"] = quiz.get_question_count()
        context["total_points"] = quiz.get_total_points()
        context["quiz_questions"] = quiz.get_questions()
        context.update(self.get_lesson_quiz_urls())

        if self.request.user.is_authenticated:
            profile = self.request.profile

            # Only show lesson-scoped attempts
            context["user_attempts"] = QuizAttempt.objects.filter(
                user=profile, quiz=quiz, lesson_quiz=lesson_quiz
            ).order_by("-start_time")[:10]

            best_attempt = (
                QuizAttempt.objects.filter(
                    user=profile,
                    quiz=quiz,
                    lesson_quiz=lesson_quiz,
                    is_submitted=True,
                )
                .order_by("-score")
                .first()
            )
            context["best_score"] = best_attempt.score if best_attempt else None

            lesson_attempt_count = QuizAttempt.objects.filter(
                user=profile,
                quiz=quiz,
                lesson_quiz=lesson_quiz,
                is_submitted=True,
            ).count()
            context["attempts_count"] = lesson_attempt_count

            in_progress = QuizAttempt.objects.filter(
                user=profile,
                quiz=quiz,
                lesson_quiz=lesson_quiz,
                is_submitted=False,
            ).first()
            context["in_progress_attempt"] = in_progress
            if in_progress:
                context["continue_url"] = reverse(
                    "lesson_quiz_take",
                    kwargs=self.get_lesson_url_kwargs(attempt_id=in_progress.id),
                )

            if lesson_quiz.max_attempts > 0:
                context["submission_limit"] = lesson_quiz.max_attempts
                context["submissions_left"] = max(
                    lesson_quiz.max_attempts - lesson_attempt_count, 0
                )

        return context


class LessonQuizStart(LessonQuizMixin, LoginRequiredMixin, View):
    """Start a new quiz attempt in lesson context."""

    def post(self, request, *args, **kwargs):
        lesson_quiz = self.get_lesson_quiz()
        quiz = lesson_quiz.quiz
        profile = request.profile

        # Check max_attempts
        if not lesson_quiz.can_attempt(request.user):
            if lesson_quiz.max_attempts > 0:
                current_attempts = lesson_quiz.get_attempts_count(request.user)
                if current_attempts >= lesson_quiz.max_attempts:
                    messages.error(
                        request,
                        _(
                            "You have reached the maximum number of attempts (%d) for this quiz."
                        )
                        % lesson_quiz.max_attempts,
                    )
                    return redirect(
                        "lesson_quiz_detail",
                        **self.get_lesson_url_kwargs(),
                    )
            messages.error(request, _("You cannot attempt this quiz."))
            return redirect(
                "lesson_quiz_detail",
                **self.get_lesson_url_kwargs(),
            )

        # Check for in-progress attempt in this lesson context
        existing = QuizAttempt.objects.filter(
            user=profile,
            quiz=quiz,
            lesson_quiz=lesson_quiz,
            is_submitted=False,
        ).first()
        if existing:
            request.session[f"quiz_attempt_{quiz.code}"] = existing.id
            return redirect(
                "lesson_quiz_take",
                **self.get_lesson_url_kwargs(attempt_id=existing.id),
            )

        # Create new attempt with lesson context
        attempt = QuizAttempt.objects.create(
            user=profile,
            quiz=quiz,
            attempt_number=QuizAttempt.objects.filter(
                user=profile, quiz=quiz, lesson_quiz=lesson_quiz
            ).count()
            + 1,
            time_limit_minutes=quiz.time_limit,
            lesson_quiz=lesson_quiz,
        )

        request.session[f"quiz_attempt_{quiz.code}"] = attempt.id
        return redirect(
            "lesson_quiz_take",
            **self.get_lesson_url_kwargs(attempt_id=attempt.id),
        )

    def get(self, request, *args, **kwargs):
        return redirect(
            "lesson_quiz_detail",
            **self.get_lesson_url_kwargs(),
        )


class LessonQuizTake(LessonQuizMixin, LoginRequiredMixin, TitleMixin, DetailView):
    """Take quiz in lesson context."""

    model = QuizAttempt
    template_name = "quiz/take.html"
    context_object_name = "attempt"
    pk_url_kwarg = "attempt_id"

    def get_title(self):
        return _("Taking: %s") % self.object.quiz.title

    def get_object(self, queryset=None):
        attempt = get_object_or_404(
            QuizAttempt,
            pk=self.kwargs["attempt_id"],
            quiz__code=self.kwargs["code"],
        )

        if attempt.user != self.request.profile:
            raise PermissionDenied(_("This is not your attempt."))

        self.validate_attempt_context(attempt)

        if attempt.is_submitted:
            return redirect(
                "lesson_quiz_result",
                **self.get_lesson_url_kwargs(attempt_id=attempt.id),
            )

        if attempt.is_expired():
            attempt.auto_submit()
            messages.info(
                self.request,
                _("Time has expired. Your quiz has been submitted automatically."),
            )
            return redirect(
                "lesson_quiz_result",
                **self.get_lesson_url_kwargs(attempt_id=attempt.id),
            )

        return attempt

    def get(self, request, *args, **kwargs):
        result = self.get_object()
        if isinstance(result, HttpResponseRedirect):
            return result
        self.object = result

        session_attempt_id = request.session.get(
            f"quiz_attempt_{self.object.quiz.code}"
        )
        if session_attempt_id and session_attempt_id != self.object.id:
            messages.warning(
                request, _("You already have another attempt in progress.")
            )

        request.session[f"quiz_attempt_{self.object.quiz.code}"] = self.object.id
        return self.render_to_response(self.get_context_data())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        quiz = attempt.quiz

        context["quiz"] = quiz
        context.update(self.get_lesson_quiz_urls(attempt_id=attempt.id))

        assignments = attempt.get_question_assignments()
        context["questions"] = assignments
        context["question_count"] = len(assignments)
        answers = QuizAnswer.objects.filter(attempt=attempt).prefetch_related("files")
        context["answers"] = {a.question_id: a for a in answers}
        context["time_remaining"] = attempt.time_remaining()
        context["has_time_limit"] = quiz.time_limit is not None and quiz.time_limit > 0

        uploaded_files = {}
        for answer in answers:
            if answer.files.exists():
                uploaded_files[answer.question_id] = [
                    {
                        "id": f.id,
                        "filename": f.original_filename,
                        "size": f.get_file_size(),
                        "url": f.file.url if f.file else None,
                        "extension": f.get_file_extension(),
                    }
                    for f in answer.files.all()
                ]
        context["uploaded_files"] = uploaded_files

        return context


class LessonQuizSaveAnswer(LessonQuizMixin, LoginRequiredMixin, View):
    """AJAX save answer in lesson context."""

    def post(self, request, *args, **kwargs):
        # Validate the attempt belongs to this lesson
        try:
            attempt = QuizAttempt.objects.get(
                id=kwargs["attempt_id"],
                user=request.profile,
                is_submitted=False,
            )
        except QuizAttempt.DoesNotExist:
            return JsonResponse({"error": "Attempt not found"}, status=404)

        self.validate_attempt_context(attempt)

        # Delegate to the original save logic
        view = QuizSaveAnswer()
        view.request = request
        view.args = args
        view.kwargs = kwargs
        return view.post(request, *args, **kwargs)


class LessonQuizUploadFile(LessonQuizMixin, LoginRequiredMixin, View):
    """AJAX file upload in lesson context."""

    def post(self, request, *args, **kwargs):
        try:
            attempt = QuizAttempt.objects.get(
                id=kwargs["attempt_id"],
                user=request.profile,
                is_submitted=False,
            )
        except QuizAttempt.DoesNotExist:
            return JsonResponse({"error": "Attempt not found"}, status=404)

        self.validate_attempt_context(attempt)

        view = QuizUploadFile()
        view.request = request
        view.args = args
        view.kwargs = kwargs
        return view.post(request, *args, **kwargs)


class LessonQuizSubmit(LessonQuizMixin, LoginRequiredMixin, View):
    """Submit quiz in lesson context."""

    def post(self, request, *args, **kwargs):
        attempt = get_object_or_404(
            QuizAttempt.objects.select_related("contest_participation"),
            pk=kwargs["attempt_id"],
            quiz__code=kwargs["code"],
            user=request.profile,
        )

        self.validate_attempt_context(attempt)

        if attempt.is_submitted:
            messages.warning(request, _("This attempt was already submitted."))
            return redirect(
                "lesson_quiz_result",
                **self.get_lesson_url_kwargs(attempt_id=attempt.id),
            )

        # Reuse the core submission logic from QuizSubmit
        time_expired = attempt.is_expired()

        with transaction.atomic():
            quiz_questions = attempt.quiz.quiz_questions.select_related("question")
            answered_question_ids = set()

            for key, value in request.POST.items():
                if key.startswith("q_"):
                    try:
                        question_id = int(key[2:])
                        question = QuizQuestion.objects.get(pk=question_id)
                        answered_question_ids.add(question_id)

                        if question.question_type == "MA":
                            values = request.POST.getlist(key)
                            answer_text = json.dumps(values)
                        else:
                            answer_text = value

                        QuizAnswer.objects.update_or_create(
                            attempt=attempt,
                            question=question,
                            defaults={"answer": answer_text},
                        )
                    except (ValueError, QuizQuestion.DoesNotExist):
                        continue

            for assignment in quiz_questions:
                if assignment.question_id not in answered_question_ids:
                    QuizAnswer.objects.get_or_create(
                        attempt=attempt,
                        question=assignment.question,
                        defaults={"answer": ""},
                    )

            attempt.end_time = timezone.now()
            attempt.is_submitted = True
            attempt.save(update_fields=["end_time", "is_submitted"])

            from judge.utils.quiz_grading import (
                auto_grade_quiz_attempt,
                notify_graders_for_essay,
            )

            auto_grade_quiz_attempt(attempt)
            notify_graders_for_essay(attempt)

        session_key = f"quiz_attempt_{attempt.quiz.code}"
        if session_key in request.session:
            del request.session[session_key]

        if time_expired:
            messages.info(
                request,
                _(
                    "Time had expired. Your answers were saved and the quiz has been submitted."
                ),
            )
        else:
            messages.success(request, _("Quiz submitted successfully!"))

        return redirect(
            "lesson_quiz_result",
            **self.get_lesson_url_kwargs(attempt_id=attempt.id),
        )

    def get(self, request, *args, **kwargs):
        return redirect(
            "lesson_quiz_take",
            **self.get_lesson_url_kwargs(attempt_id=kwargs["attempt_id"]),
        )


class LessonQuizResult(LessonQuizMixin, LoginRequiredMixin, TitleMixin, DetailView):
    """View quiz results in lesson context."""

    model = QuizAttempt
    template_name = "quiz/result.html"
    context_object_name = "attempt"
    pk_url_kwarg = "attempt_id"

    def get_title(self):
        return _("Results: %s") % self.object.quiz.title

    def get_object(self, queryset=None):
        attempt = get_object_or_404(
            QuizAttempt.objects.select_related("quiz", "user"),
            pk=self.kwargs["attempt_id"],
            quiz__code=self.kwargs["code"],
        )

        is_owner = attempt.user == self.request.profile
        self._is_editor = attempt.quiz.is_editable_by(self.request.user)
        if not (is_owner or self._is_editor):
            raise PermissionDenied(_("You cannot view this result."))

        self.validate_attempt_context(attempt)
        return attempt

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        attempt = self.object
        quiz = attempt.quiz
        is_editor = self._is_editor

        context.update(self.get_lesson_quiz_urls(attempt_id=attempt.id))

        answers = list(
            QuizAnswer.objects.filter(attempt=attempt)
            .select_related("question")
            .prefetch_related("files")
            .order_by("question__id")
        )

        # Batch-load max points to avoid N+1 queries from get_max_points()
        question_ids = [a.question_id for a in answers]
        assignments = QuizQuestionAssignment.objects.filter(
            quiz=quiz, question_id__in=question_ids
        )
        max_points_map = {a.question_id: a.points for a in assignments}
        for answer in answers:
            answer._max_points = max_points_map.get(answer.question_id, 1.0)

        context["answers"] = answers
        context["show_answers"] = is_editor or quiz.is_shown_answer
        context["show_correctness"] = is_editor or quiz.is_shown_correctness
        context["can_edit"] = is_editor

        # Check if results should be hidden in lesson context
        lesson_quiz = self.get_lesson_quiz()
        context["is_result_hidden"] = quiz.should_hide_result(
            self.request.user, lesson_quiz=lesson_quiz
        )

        # Check if user can retake in this lesson context
        can_retake = (
            attempt.user == self.request.profile
            and lesson_quiz.can_attempt(self.request.user)
            and not QuizAttempt.objects.filter(
                quiz=quiz,
                user=self.request.profile,
                lesson_quiz=lesson_quiz,
                is_submitted=False,
            ).exists()
        )
        context["can_retake"] = can_retake

        if attempt.max_score and attempt.max_score > 0:
            context["percentage"] = round(
                (attempt.score or 0) / attempt.max_score * 100, 1
            )
        else:
            context["percentage"] = 0

        return context


class LessonQuizAttemptList(LessonQuizMixin, LoginRequiredMixin, TitleMixin, ListView):
    """View attempts for a quiz in lesson context (only lesson-scoped attempts)."""

    model = QuizAttempt
    template_name = "quiz/attempt_list.html"
    context_object_name = "attempts"
    paginate_by = 20

    def get_title(self):
        return _("My Attempts: %s") % self.get_lesson_quiz().quiz.title

    def get_queryset(self):
        lesson_quiz = self.get_lesson_quiz()
        return QuizAttempt.objects.filter(
            quiz=lesson_quiz.quiz,
            user=self.request.profile,
            lesson_quiz=lesson_quiz,
        ).order_by("-start_time")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        lesson_quiz = self.get_lesson_quiz()
        quiz = lesson_quiz.quiz

        context["quiz"] = quiz
        context["can_edit"] = quiz.is_editable_by(self.request.user)
        context["total_points"] = quiz.get_total_points()
        context["target_profile"] = self.request.profile
        context["viewing_other_user"] = False
        context.update(self.get_lesson_quiz_urls())

        # Best score from lesson-scoped attempts only
        best_attempt = (
            self.get_queryset().filter(is_submitted=True).order_by("-score").first()
        )
        context["best_score"] = best_attempt.score if best_attempt else None

        # Check if results should be hidden in lesson context
        context["is_result_hidden"] = quiz.should_hide_result(
            self.request.user, lesson_quiz=lesson_quiz
        )

        context.update(paginate_query_context(self.request))
        return context
