import threading

from django.urls import re_path
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, transaction
from django.db.models import Q, TextField
from django.forms import ModelForm, ModelMultipleChoiceField, TextInput
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _, ngettext
import reversion
from reversion_compare.admin import CompareVersionAdmin

from django_ace import AceWidget
from judge.models import (
    Contest,
    ContestProblem,
    ContestPublicRequest,
    ContestReviewRun,
    ContestSubmission,
    Profile,
    Rating,
    OfficialContest,
)
from judge.auto_review.publication import publish_without_review
from judge.auto_review.verdict import batched_verdicts
from judge.ratings import rate_contest
from judge.utils.formsets import active_formset_forms
from judge.utils.identity import SemanticIdentityInlineFormSet
from judge.widgets import (
    AdminHeavySelect2MultipleWidget,
    AdminHeavySelect2Widget,
    AdminPagedownWidget,
    AdminSelect2MultipleWidget,
    AdminSelect2Widget,
    HeavyPreviewAdminPageDownWidget,
)
from judge.views.contests import recalculate_contest_summary_result
from judge.utils.contest import maybe_trigger_contest_rescore
from judge.utils.public_visibility import is_site_public
from judge.services.contest_summary import validate_scores

MAX_CONTEST_PROBLEMS = 100
MAX_CONTEST_QUIZZES = 100


class AdminHeavySelect2Widget(AdminHeavySelect2Widget):
    @property
    def is_hidden(self):
        return False


class ContestTagForm(ModelForm):
    contests = ModelMultipleChoiceField(
        label=_("Included contests"),
        queryset=Contest.objects.all(),
        required=False,
        widget=AdminHeavySelect2MultipleWidget(data_view="contest_select2"),
    )


class ContestTagAdmin(admin.ModelAdmin):
    fields = ("name", "color", "description", "contests")
    list_display = ("name", "color")
    actions_on_top = True
    actions_on_bottom = True
    form = ContestTagForm

    if AdminPagedownWidget is not None:
        formfield_overrides = {
            TextField: {"widget": AdminPagedownWidget},
        }

    def save_model(self, request, obj, form, change):
        super(ContestTagAdmin, self).save_model(request, obj, form, change)
        obj.contests.set(form.cleaned_data["contests"])

    def get_form(self, request, obj=None, **kwargs):
        form = super(ContestTagAdmin, self).get_form(request, obj, **kwargs)
        if obj is not None:
            form.base_fields["contests"].initial = obj.contests.all()
        return form


class ContestProblemInlineForm(ModelForm):
    def save(self, commit=True):
        instance = super().save(commit=False)
        if commit:
            # ContestAdmin.save_related() schedules one rescore after all inline
            # rows are saved. Avoid recomputing every participation once here and
            # then repeating the same work in the background task.
            instance.save(recompute_participations=False)
            self.save_m2m()
        return instance

    class Meta:
        widgets = {
            "problem": AdminHeavySelect2Widget(data_view="problem_select2"),
            "quiz": AdminSelect2Widget,
            "hidden_subtasks": TextInput(attrs={"size": "3"}),
            "points": TextInput(attrs={"size": "1"}),
            "order": TextInput(attrs={"size": "1"}),
        }


class ContestProblemInlineFormSet(SemanticIdentityInlineFormSet):
    semantic_identity_fields = ("problem", "quiz")

    def is_valid(self):
        valid = super().is_valid()
        if not valid:
            return valid

        problem_forms, quiz_forms = [], []
        for form in active_formset_forms(self):
            if form.cleaned_data.get("problem"):
                problem_forms.append(form)
            if form.cleaned_data.get("quiz"):
                quiz_forms.append(form)

        if len(problem_forms) > MAX_CONTEST_PROBLEMS:
            problem_forms[MAX_CONTEST_PROBLEMS].add_error(
                None,
                _("A contest may contain at most %(count)d problems.")
                % {"count": MAX_CONTEST_PROBLEMS},
            )
            return False
        if len(quiz_forms) > MAX_CONTEST_QUIZZES:
            quiz_forms[MAX_CONTEST_QUIZZES].add_error(
                None,
                _("A contest may contain at most %(count)d quizzes.")
                % {"count": MAX_CONTEST_QUIZZES},
            )
            return False
        return True


class ContestProblemInline(admin.TabularInline):
    model = ContestProblem
    verbose_name = _("Problem")
    verbose_name_plural = "Problems"
    fields = (
        "problem",
        "quiz",
        "points",
        "partial",
        "is_pretested",
        "max_submissions",
        "hidden_subtasks",
        "is_result_hidden",
        "show_testcases",
        "order",
        "rejudge_column",
    )
    readonly_fields = ("rejudge_column",)
    form = ContestProblemInlineForm
    formset = ContestProblemInlineFormSet
    max_num = MAX_CONTEST_PROBLEMS + MAX_CONTEST_QUIZZES

    def rejudge_column(self, obj):
        if obj.id is None:
            return ""
        return format_html(
            '<a class="button rejudge-link" href="{}">Rejudge</a>',
            reverse("admin:judge_contest_rejudge", args=(obj.contest.id, obj.id)),
        )

    rejudge_column.short_description = ""


class ContestForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(ContestForm, self).__init__(*args, **kwargs)
        if "rate_exclude" in self.fields:
            if self.instance and self.instance.id:
                self.fields["rate_exclude"].queryset = Profile.objects.filter(
                    contest_history__contest=self.instance
                ).distinct()
            else:
                self.fields["rate_exclude"].queryset = Profile.objects.none()
        self.fields["banned_users"].widget.can_add_related = False
        self.fields["view_contest_scoreboard"].widget.can_add_related = False

    class Meta:
        widgets = {
            "authors": AdminHeavySelect2MultipleWidget(data_view="profile_select2"),
            "curators": AdminHeavySelect2MultipleWidget(data_view="profile_select2"),
            "testers": AdminHeavySelect2MultipleWidget(data_view="profile_select2"),
            "private_contestants": AdminHeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "organizations": AdminHeavySelect2MultipleWidget(
                data_view="organization_select2"
            ),
            "tags": AdminSelect2MultipleWidget,
            "banned_users": AdminHeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
            "view_contest_scoreboard": AdminHeavySelect2MultipleWidget(
                data_view="profile_select2", attrs={"style": "width: 100%"}
            ),
        }

        if HeavyPreviewAdminPageDownWidget is not None:
            widgets["description"] = HeavyPreviewAdminPageDownWidget(
                preview=reverse_lazy("contest_preview")
            )


class OfficialContestInlineForm(ModelForm):
    class Meta:
        widgets = {
            "category": AdminSelect2Widget,
            "location": AdminSelect2Widget,
        }


class OfficialContestInline(admin.StackedInline):
    fields = (
        "category",
        "year",
        "location",
    )
    model = OfficialContest
    can_delete = True
    form = OfficialContestInlineForm
    extra = 0


class ContestAdmin(CompareVersionAdmin):
    fieldsets = (
        (None, {"fields": ("key", "name", "authors", "curators", "testers")}),
        (
            _("Settings"),
            {
                "fields": (
                    "is_visible",
                    "is_locked",
                    "use_clarifications",
                    "hide_problem_tags",
                    "public_scoreboard",
                    "scoreboard_visibility",
                    "run_pretests_only",
                    "points_precision",
                    "rate_limit",
                )
            },
        ),
        (
            _("Scheduling"),
            {"fields": ("start_time", "end_time", "time_limit", "freeze_after")},
        ),
        (
            _("Details"),
            {
                "fields": (
                    "description",
                    "og_image",
                    "logo_override_image",
                    "tags",
                    "summary",
                )
            },
        ),
        (
            _("Format"),
            {"fields": ("format_name", "format_config", "problem_label_script")},
        ),
        (
            _("Rating"),
            {
                "fields": (
                    "is_rated",
                    "rate_all",
                    "rating_floor",
                    "rating_ceiling",
                    "rate_exclude",
                )
            },
        ),
        (
            _("Access"),
            {
                "fields": (
                    "access_code",
                    "private_contestants",
                    "organizations",
                    "view_contest_scoreboard",
                )
            },
        ),
        (_("Justice"), {"fields": ("banned_users",)}),
    )
    list_display = (
        "key",
        "name",
        "is_visible",
        "is_rated",
        "start_time",
        "end_time",
        "time_limit",
        "user_count",
    )
    search_fields = ("key", "name")
    inlines = [ContestProblemInline, OfficialContestInline]
    actions_on_top = True
    actions_on_bottom = True
    form = ContestForm
    change_list_template = "admin/judge/contest/change_list.html"
    filter_horizontal = ["rate_exclude"]
    date_hierarchy = "start_time"

    def get_actions(self, request):
        actions = super(ContestAdmin, self).get_actions(request)

        if request.user.has_perm(
            "judge.change_contest_visibility"
        ) or request.user.has_perm("judge.create_private_contest"):
            actions["make_hidden"] = self.get_action("make_hidden")
        return actions

    def get_queryset(self, request):
        queryset = Contest.objects.all()
        if request.user.has_perm("judge.edit_all_contest"):
            return queryset
        else:
            return queryset.filter(
                Q(authors=request.profile) | Q(curators=request.profile)
            ).distinct()

    def get_readonly_fields(self, request, obj=None):
        readonly = []
        if not request.user.is_superuser:
            readonly += ["is_locked"]
        if not request.user.is_superuser:
            readonly += ["is_visible"]
        if not request.user.has_perm("judge.contest_rating"):
            readonly += ["is_rated", "rate_all", "rate_exclude"]
        if not request.user.has_perm("judge.contest_access_code"):
            readonly += ["access_code"]
        if not request.user.has_perm("judge.create_private_contest"):
            readonly += [
                "private_contestants",
                "organizations",
            ]
            if not request.user.has_perm("judge.change_contest_visibility"):
                readonly += ["is_visible"]
        if not request.user.has_perm("judge.contest_problem_label"):
            readonly += ["problem_label_script"]
        return readonly

    def save_model(self, request, obj, form, change):
        request._contest_was_site_public = bool(
            change and is_site_public(Contest.objects.get(pk=obj.pk))
        )
        # `is_visible` will not appear in `cleaned_data` if user cannot edit it
        if form.cleaned_data.get("is_visible") and not request.user.has_perm(
            "judge.change_contest_visibility"
        ):
            if (
                not len(form.cleaned_data["organizations"]) > 0
                and not len(form.cleaned_data["private_contestants"]) > 0
            ):
                raise PermissionDenied
            if not request.user.has_perm("judge.create_private_contest"):
                raise PermissionDenied

        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Validation must not modify participation, and a new contest has no PK
        # until save_model has run. Apply bans only after all forms are accepted.
        form.instance.banned_users.filter(
            current_contest__contest_id=form.instance.pk
        ).update(current_contest=None)
        if request.user.is_superuser and not getattr(
            request, "_contest_was_site_public", False
        ):
            obj = form.instance
            obj.refresh_from_db()
            if obj.is_visible and not (
                obj.is_private or obj.is_organization_private or obj.is_in_course
            ):
                try:
                    publish_without_review(
                        obj,
                        request.user,
                        _("Published directly by a superuser from Admin."),
                    )
                except ValidationError as error:
                    raise PermissionDenied(" ".join(error.messages)) from error
                obj.refresh_from_db()
        # Only rescored if we did not already do so in `save_model`
        formset_changed = False
        if any(formset.has_changed() for formset in formsets):
            formset_changed = True

        maybe_trigger_contest_rescore(form, form.instance, formset_changed)

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm("judge.edit_own_contest"):
            return False
        if obj is None:
            return True
        return obj.is_editable_by(request.user)

    def has_delete_permission(self, request, obj=None):
        return not (
            obj is not None and obj.is_locked and not request.user.is_superuser
        ) and super().has_delete_permission(request, obj)

    def make_hidden(self, request, queryset):
        if not request.user.is_superuser:
            queryset = queryset.filter(is_locked=False)
        if not request.user.has_perm("judge.change_contest_visibility"):
            queryset = queryset.filter(
                Q(is_private=True) | Q(is_organization_private=True)
            )
        count = queryset.update(is_visible=False)
        self.message_user(
            request,
            ngettext(
                "%d contest successfully marked as hidden.",
                "%d contests successfully marked as hidden.",
                count,
            )
            % count,
        )

    make_hidden.short_description = _("Mark contests as hidden")

    def get_urls(self):
        return [
            re_path(r"^rate/all/$", self.rate_all_view, name="judge_contest_rate_all"),
            re_path(r"^(\d+)/rate/$", self.rate_view, name="judge_contest_rate"),
            re_path(
                r"^(\d+)/judge/(\d+)/$", self.rejudge_view, name="judge_contest_rejudge"
            ),
        ] + super(ContestAdmin, self).get_urls()

    def rejudge_view(self, request, contest_id, problem_id):
        queryset = ContestSubmission.objects.filter(
            problem_id=problem_id
        ).select_related("submission")
        for model in queryset:
            model.submission.judge(rejudge=True)

        self.message_user(
            request,
            ngettext(
                "%d submission was successfully scheduled for rejudging.",
                "%d submissions were successfully scheduled for rejudging.",
                len(queryset),
            )
            % len(queryset),
        )
        return HttpResponseRedirect(
            reverse("admin:judge_contest_change", args=(contest_id,))
        )

    def log_contests_rated(self, request, contests, comment):
        # Record who rated which contest (and when) on the contest's /history/
        # page. That page is rendered by reversion-compare from reversion
        # revisions (not Django's LogEntry table), so we snapshot each rated
        # contest into a single revision tagged with the acting user and a
        # comment. The rate views call contest.rate()/rate_contest() directly,
        # which never touches the Contest row, so without this nothing is logged.
        if not contests:
            return
        with reversion.create_revision():
            reversion.set_user(request.user)
            reversion.set_comment(comment)
            for contest in contests:
                reversion.add_to_revision(contest)

    def rate_all_view(self, request):
        if not request.user.has_perm("judge.contest_rating"):
            raise PermissionDenied()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE TABLE `%s`" % Rating._meta.db_table)
            Profile.objects.update(rating=None)
            rated = list(
                Contest.objects.filter(
                    is_rated=True, end_time__lte=timezone.now()
                ).order_by("end_time")
            )
            for contest in rated:
                rate_contest(contest)
            self.log_contests_rated(
                request, rated, _("Rated via “Rate all ratable contests”.")
            )
        return HttpResponseRedirect(reverse("admin:judge_contest_changelist"))

    def rate_view(self, request, id):
        if not request.user.has_perm("judge.contest_rating"):
            raise PermissionDenied()
        contest = get_object_or_404(Contest, id=id)
        if not contest.is_rated or not contest.ended:
            raise Http404()
        with transaction.atomic():
            rated = contest.rate()
            self.log_contests_rated(request, rated, _("Rated this contest."))
        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER", reverse("admin:judge_contest_changelist"))
        )

    def get_form(self, request, obj=None, **kwargs):
        form = super(ContestAdmin, self).get_form(request, obj, **kwargs)
        if "problem_label_script" in form.base_fields:
            # form.base_fields['problem_label_script'] does not exist when the user has only view permission
            # on the model.
            form.base_fields["problem_label_script"].widget = AceWidget(
                "lua", request.profile.ace_theme
            )

        perms = ("edit_own_contest", "edit_all_contest")
        form.base_fields["curators"].queryset = Profile.objects.filter(
            Q(user__is_superuser=True)
            | Q(user__groups__permissions__codename__in=perms)
            | Q(user__user_permissions__codename__in=perms),
        ).distinct()
        return form


class ContestParticipationForm(ModelForm):
    class Meta:
        widgets = {
            "contest": AdminSelect2Widget(),
            "user": AdminHeavySelect2Widget(data_view="profile_select2"),
        }


class ContestParticipationAdmin(admin.ModelAdmin):
    fields = ("contest", "user", "real_start", "virtual", "is_disqualified")
    list_display = (
        "contest",
        "username",
        "show_virtual",
        "real_start",
        "score",
        "cumtime",
        "tiebreaker",
    )
    actions = ["recalculate_results"]
    actions_on_bottom = actions_on_top = True
    search_fields = ("contest__key", "contest__name", "user__user__username")
    form = ContestParticipationForm
    date_hierarchy = "real_start"

    def get_queryset(self, request):
        return (
            super(ContestParticipationAdmin, self)
            .get_queryset(request)
            .only(
                "contest__name",
                "contest__format_name",
                "contest__format_config",
                "user__user__username",
                "real_start",
                "score",
                "cumtime",
                "tiebreaker",
                "virtual",
            )
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if form.changed_data and "is_disqualified" in form.changed_data:
            obj.set_disqualified(obj.is_disqualified)

    def recalculate_results(self, request, queryset):
        count = 0
        for participation in queryset:
            participation.recompute_results()
            count += 1
        self.message_user(
            request,
            ngettext(
                "%d participation recalculated.",
                "%d participations recalculated.",
                count,
            )
            % count,
        )

    recalculate_results.short_description = _("Recalculate results")

    def username(self, obj):
        return obj.user.username

    username.short_description = _("username")
    username.admin_order_field = "user__user__username"

    def show_virtual(self, obj):
        return obj.virtual or "-"

    show_virtual.short_description = _("virtual")
    show_virtual.admin_order_field = "virtual"


class ContestsSummaryForm(ModelForm):
    def clean_scores(self):
        scores = self.cleaned_data["scores"]
        validate_scores(scores)
        return scores

    class Meta:
        widgets = {
            "contests": AdminHeavySelect2MultipleWidget(
                data_view="contest_select2", attrs={"style": "width: 100%"}
            ),
        }


class ContestsSummaryAdmin(admin.ModelAdmin):
    fields = ("key", "contests", "scores")
    list_display = ("key",)
    search_fields = ("key", "contests__key")
    form = ContestsSummaryForm
    actions = ("refresh_results",)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        obj = form.instance
        obj.results = recalculate_contest_summary_result(request, obj)
        obj.save(update_fields=["results"])

    @admin.action(description=_("Refresh summary results"))
    def refresh_results(self, request, queryset):
        for summary in queryset:
            try:
                with transaction.atomic():
                    locked = (
                        type(summary).objects.select_for_update().get(pk=summary.pk)
                    )
                    result = recalculate_contest_summary_result(request, locked)
                    locked.results = result
                    locked.save(update_fields=["results"])
            except ValidationError as error:
                self.message_user(
                    request,
                    "%s: %s" % (summary.key, "; ".join(error.messages)),
                    level=messages.ERROR,
                )


# Per-request scratch space for the verdict pre-fetch cache. ModelAdmin
# instances are singletons within a process (Django instantiates them once
# at startup); storing per-request data on `self` would race across
# concurrent admin requests. threading.local() gives each request thread
# its own storage. Cleared in `changelist_view`'s finally block.
_admin_threadlocal = threading.local()


class ContestPublicRequestAdmin(admin.ModelAdmin):
    """Admin queue for incoming contest public requests.

    Shows the latest auto-review verdict per row so admins can triage without
    opening each row. The verdict is bulk-fetched per page (N+1-safe) using
    the same pattern as ProblemPublicRequest's _verdict_for in internal.py.
    """

    list_display = (
        "contest",
        "requested_by",
        "status",
        "review_verdict",
        "created_at",
        "updated_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("contest__key", "contest__name", "requested_by__user__username")
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("contest", "requested_by", "reviewed_by")
    actions = None  # Publication decisions use the review workflow.

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("contest", "requested_by__user", "reviewed_by__user")
        )

    def changelist_view(self, request, extra_context=None):
        """Batch saved assessments; avoid per-row queries and duplicate verdict rules."""
        contest_ids = list(
            self.get_queryset(request).values_list("contest_id", flat=True)
        )
        _latest, verdicts = batched_verdicts(
            contest_ids, ContestReviewRun, "contest_id"
        )
        _admin_threadlocal.verdicts = verdicts
        try:
            return super().changelist_view(request, extra_context)
        finally:
            del _admin_threadlocal.verdicts

    def review_verdict(self, obj):
        labels = {
            "pass": _("Pass"),
            "warning": _("Warning"),
            "fail": _("Fail"),
            "manual": _("Manual review"),
            "running": _("Running"),
            "error": _("Error"),
            "incomplete": _("Incomplete"),
            "stale": _("Needs re-review"),
            "cancelled": _("Cancelled"),
        }
        verdict = getattr(_admin_threadlocal, "verdicts", {}).get(obj.contest_id)
        return labels.get(verdict, "—")

    review_verdict.short_description = _("Auto-review")


admin.site.register(ContestPublicRequest, ContestPublicRequestAdmin)
