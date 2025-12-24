from operator import attrgetter

from django import forms
from django.contrib import admin
from django.db.models import Q
from django.forms import ModelForm
from django.urls import reverse, reverse_lazy
from django.utils.html import format_html
from django.utils.translation import gettext, gettext_lazy as _, ngettext
from reversion_compare.admin import CompareVersionAdmin

from judge.models import (
    Quiz,
    QuizQuestion,
    QuizQuestionAssignment,
    CourseLessonQuiz,
    QuizAttempt,
    QuizAnswer,
    QuizAnswerFile,
    Profile,
)
from judge.widgets import (
    AdminHeavySelect2MultipleWidget,
    AdminHeavySelect2Widget,
    AdminSelect2Widget,
    HeavyPreviewAdminPageDownWidget,
)


# =============================================================================
# QuizQuestion Admin
# =============================================================================


class QuizQuestionForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(QuizQuestionForm, self).__init__(*args, **kwargs)
        self.fields["authors"].widget.can_add_related = False
        self.fields["curators"].widget.can_add_related = False

    class Meta:
        widgets = {
            "authors": AdminHeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "curators": AdminHeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
        }
        if HeavyPreviewAdminPageDownWidget is not None:
            widgets["content"] = HeavyPreviewAdminPageDownWidget(
                preview=reverse_lazy("blog_preview")
            )
            widgets["explanation"] = HeavyPreviewAdminPageDownWidget(
                preview=reverse_lazy("blog_preview")
            )


class QuizQuestionCreatorListFilter(admin.SimpleListFilter):
    title = parameter_name = "creator"

    def lookups(self, request, model_admin):
        queryset = Profile.objects.exclude(authored_quiz_questions=None).values_list(
            "user__username", flat=True
        )
        return [(name, name) for name in queryset]

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        return queryset.filter(authors__user__username=self.value())


class QuizQuestionAdmin(CompareVersionAdmin):
    fieldsets = (
        (
            _("Basic Information"),
            {
                "fields": (
                    "question_type",
                    "title",
                    "content",
                    "is_public",
                ),
            },
        ),
        (
            _("Answer Configuration"),
            {
                "fields": (
                    "choices",
                    "correct_answers",
                    "shuffle_choices",
                ),
            },
        ),
        (
            _("Points & Feedback"),
            {
                "fields": (
                    "default_points",
                    "explanation",
                ),
            },
        ),
        (
            _("Categorization"),
            {
                "fields": ("tags",),
            },
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "authors",
                    "curators",
                ),
            },
        ),
    )
    list_display = [
        "id",
        "title",
        "question_type",
        "show_authors",
        "is_public",
        "created_at",
    ]
    list_display_links = ["id", "title"]
    ordering = ["-created_at"]
    search_fields = (
        "title",
        "content",
        "tags",
        "authors__user__username",
    )
    list_filter = ("question_type", "is_public", QuizQuestionCreatorListFilter)
    form = QuizQuestionForm
    date_hierarchy = "created_at"
    actions_on_top = True
    actions_on_bottom = True

    def show_authors(self, obj):
        return ", ".join(map(attrgetter("user.username"), obj.authors.all()))

    show_authors.short_description = _("Authors")

    def get_queryset(self, request):
        queryset = QuizQuestion.objects.prefetch_related("authors__user")
        if request.user.has_perm("judge.edit_all_problem"):
            return queryset

        access = Q()
        if request.user.has_perm("judge.edit_own_problem"):
            access |= Q(authors__id=request.profile.id) | Q(
                curators__id=request.profile.id
            )
        return queryset.filter(access).distinct() if access else queryset.none()

    def has_change_permission(self, request, obj=None):
        if request.user.has_perm("judge.edit_all_problem") or obj is None:
            return True
        if not request.user.has_perm("judge.edit_own_problem"):
            return False
        return obj.is_editor(request.profile)

    def get_form(self, *args, **kwargs):
        form = super(QuizQuestionAdmin, self).get_form(*args, **kwargs)
        form.base_fields["authors"].queryset = Profile.objects.all()
        return form

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        # Add the creator as a curator if not already an author
        if not obj.authors.filter(id=request.profile.id).exists():
            obj.curators.add(request.profile)


# =============================================================================
# Quiz Admin
# =============================================================================


class QuizQuestionAssignmentInlineForm(ModelForm):
    class Meta:
        widgets = {
            "question": AdminHeavySelect2Widget(
                data_view="quiz_question_select2", attrs={"style": "width: 100%"}
            ),
        }


class QuizQuestionAssignmentInline(admin.TabularInline):
    model = QuizQuestionAssignment
    form = QuizQuestionAssignmentInlineForm
    extra = 1
    fields = ("question", "points", "order")
    ordering = ("order", "id")


class QuizForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(QuizForm, self).__init__(*args, **kwargs)
        self.fields["authors"].widget.can_add_related = False
        self.fields["curators"].widget.can_add_related = False

    class Meta:
        widgets = {
            "authors": AdminHeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "curators": AdminHeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
        }
        if HeavyPreviewAdminPageDownWidget is not None:
            widgets["description"] = HeavyPreviewAdminPageDownWidget(
                preview=reverse_lazy("blog_preview")
            )


class QuizCreatorListFilter(admin.SimpleListFilter):
    title = parameter_name = "creator"

    def lookups(self, request, model_admin):
        queryset = Profile.objects.exclude(authored_quizzes=None).values_list(
            "user__username", flat=True
        )
        return [(name, name) for name in queryset]

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        return queryset.filter(authors__user__username=self.value())


class QuizAdmin(CompareVersionAdmin):
    fieldsets = (
        (
            _("Basic Information"),
            {
                "fields": (
                    "code",
                    "title",
                    "description",
                ),
            },
        ),
        (
            _("Quiz Settings"),
            {
                "fields": (
                    "time_limit",
                    "shuffle_questions",
                    "is_shown_answer",
                ),
            },
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "authors",
                    "curators",
                ),
            },
        ),
    )
    list_display = [
        "code",
        "title",
        "show_authors",
        "time_limit",
        "shuffle_questions",
        "is_shown_answer",
        "created_at",
        "show_public",
    ]
    list_display_links = ["code", "title"]
    ordering = ["-created_at"]
    search_fields = (
        "code",
        "title",
        "authors__user__username",
    )
    list_filter = ("shuffle_questions", "is_shown_answer", QuizCreatorListFilter)
    form = QuizForm
    date_hierarchy = "created_at"
    inlines = [QuizQuestionAssignmentInline]
    actions_on_top = True
    actions_on_bottom = True

    def show_authors(self, obj):
        return ", ".join(map(attrgetter("user.username"), obj.authors.all()))

    show_authors.short_description = _("Authors")

    def show_public(self, obj):
        try:
            url = obj.get_absolute_url()
            return format_html('<a href="{1}">{0}</a>', gettext("View on site"), url)
        except Exception:
            # URL pattern not yet created (Phase 3)
            return "-"

    show_public.short_description = ""

    def get_queryset(self, request):
        queryset = Quiz.objects.prefetch_related("authors__user")
        if request.user.has_perm("judge.edit_all_problem"):
            return queryset

        access = Q()
        if request.user.has_perm("judge.edit_own_problem"):
            access |= Q(authors__id=request.profile.id) | Q(
                curators__id=request.profile.id
            )
        return queryset.filter(access).distinct() if access else queryset.none()

    def has_change_permission(self, request, obj=None):
        if request.user.has_perm("judge.edit_all_problem") or obj is None:
            return True
        if not request.user.has_perm("judge.edit_own_problem"):
            return False
        return obj.is_editor(request.profile)

    def get_form(self, *args, **kwargs):
        form = super(QuizAdmin, self).get_form(*args, **kwargs)
        form.base_fields["authors"].queryset = Profile.objects.all()
        return form

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        # Add the creator as a curator if not already an author
        if not obj.authors.filter(id=request.profile.id).exists():
            obj.curators.add(request.profile)


# =============================================================================
# CourseLessonQuiz Admin
# =============================================================================


class CourseLessonQuizForm(ModelForm):
    class Meta:
        widgets = {
            "lesson": AdminHeavySelect2Widget(
                data_view="course_lesson_select2", attrs={"style": "width: 100%"}
            ),
            "quiz": AdminHeavySelect2Widget(
                data_view="quiz_select2", attrs={"style": "width: 100%"}
            ),
        }


class CourseLessonQuizAdmin(CompareVersionAdmin):
    list_display = ["lesson", "quiz", "max_attempts", "points", "order", "is_visible"]
    list_display_links = ["lesson", "quiz"]
    list_filter = ("is_visible",)
    search_fields = ("lesson__title", "quiz__title", "quiz__code")
    form = CourseLessonQuizForm
    ordering = ["lesson", "order"]


# =============================================================================
# QuizAttempt Admin
# =============================================================================


class QuizAnswerInline(admin.TabularInline):
    model = QuizAnswer
    extra = 0
    fields = (
        "question",
        "answer",
        "is_correct",
        "points",
        "partial_credit",
        "graded_at",
    )
    readonly_fields = ("question", "answer", "graded_at")
    ordering = ("question__id",)
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class QuizAttemptAdmin(CompareVersionAdmin):
    list_display = [
        "id",
        "user_link",
        "quiz_link",
        "attempt_number",
        "start_time",
        "end_time",
        "is_submitted",
        "score",
        "max_score",
    ]
    list_display_links = ["id"]
    list_filter = ("is_submitted", "quiz")
    search_fields = (
        "user__user__username",
        "quiz__title",
        "quiz__code",
    )
    readonly_fields = (
        "user",
        "quiz",
        "contest_participation",
        "lesson_quiz",
        "attempt_number",
        "start_time",
        "time_limit_minutes",
    )
    ordering = ["-start_time"]
    date_hierarchy = "start_time"
    inlines = [QuizAnswerInline]

    def user_link(self, obj):
        url = reverse("admin:judge_profile_change", args=[obj.user.id])
        return format_html('<a href="{}">{}</a>', url, obj.user.user.username)

    user_link.short_description = _("User")
    user_link.admin_order_field = "user__user__username"

    def quiz_link(self, obj):
        url = reverse("admin:judge_quiz_change", args=[obj.quiz.id])
        return format_html('<a href="{}">{}</a>', url, obj.quiz.title)

    quiz_link.short_description = _("Quiz")
    quiz_link.admin_order_field = "quiz__title"

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related(
                "user__user", "quiz", "contest_participation", "lesson_quiz"
            )
        )


# =============================================================================
# QuizAnswer Admin
# =============================================================================


class QuizAnswerFileInline(admin.TabularInline):
    model = QuizAnswerFile
    extra = 0
    fields = ("file", "original_filename", "uploaded_at")
    readonly_fields = ("uploaded_at",)


class QuizAnswerAdmin(CompareVersionAdmin):
    list_display = [
        "id",
        "attempt_link",
        "question_link",
        "answer_preview",
        "is_correct",
        "points",
        "graded_at",
    ]
    list_display_links = ["id"]
    list_filter = ("is_correct", "question__question_type")
    search_fields = (
        "attempt__user__user__username",
        "question__title",
        "answer",
    )
    readonly_fields = ("attempt", "question", "answered_at")
    ordering = ["-answered_at"]
    inlines = [QuizAnswerFileInline]

    def attempt_link(self, obj):
        url = reverse("admin:judge_quizattempt_change", args=[obj.attempt.id])
        return format_html('<a href="{}">{}</a>', url, str(obj.attempt))

    attempt_link.short_description = _("Attempt")

    def question_link(self, obj):
        url = reverse("admin:judge_quizquestion_change", args=[obj.question.id])
        return format_html('<a href="{}">{}</a>', url, obj.question.title)

    question_link.short_description = _("Question")

    def answer_preview(self, obj):
        answer = obj.answer
        if len(answer) > 50:
            return answer[:50] + "..."
        return answer

    answer_preview.short_description = _("Answer")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("attempt__user__user", "attempt__quiz", "question")
        )


# =============================================================================
# QuizAnswerFile Admin
# =============================================================================


class QuizAnswerFileAdmin(admin.ModelAdmin):
    list_display = ["id", "answer", "original_filename", "get_file_size", "uploaded_at"]
    list_display_links = ["id", "original_filename"]
    search_fields = ("original_filename", "answer__attempt__user__user__username")
    readonly_fields = ("uploaded_at",)
    ordering = ["-uploaded_at"]

    def get_queryset(self, request):
        return (
            super().get_queryset(request).select_related("answer__attempt__user__user")
        )
