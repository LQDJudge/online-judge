import threading

from django.urls import re_path
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Q, TextField
from django.forms import ModelForm, ModelMultipleChoiceField, TextInput
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _, ngettext
from reversion_compare.admin import CompareVersionAdmin

from django_ace import AceWidget
from judge.models import (
    Contest,
    ContestProblem,
    ContestPublicRequest,
    ContestSubmission,
    Profile,
    Rating,
    OfficialContest,
)
from judge.ratings import rate_contest
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
    class Meta:
        widgets = {
            "problem": AdminHeavySelect2Widget(data_view="problem_select2"),
            "quiz": AdminSelect2Widget,
            "hidden_subtasks": TextInput(attrs={"size": "3"}),
            "points": TextInput(attrs={"size": "1"}),
            "order": TextInput(attrs={"size": "1"}),
        }


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

    def clean(self):
        cleaned_data = super(ContestForm, self).clean()
        cleaned_data["banned_users"].filter(
            current_contest__contest=self.instance
        ).update(current_contest=None)

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
            for action in ("make_visible", "make_hidden"):
                actions[action] = self.get_action(action)

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

    def make_visible(self, request, queryset):
        if not request.user.has_perm("judge.change_contest_visibility"):
            queryset = queryset.filter(
                Q(is_private=True) | Q(is_organization_private=True)
            )
        count = queryset.update(is_visible=True)
        self.message_user(
            request,
            ngettext(
                "%d contest successfully marked as visible.",
                "%d contests successfully marked as visible.",
                count,
            )
            % count,
        )

    make_visible.short_description = _("Mark contests as visible")

    def make_hidden(self, request, queryset):
        if not request.user.has_perm("judge.change_contest_visibility"):
            queryset = queryset.filter(
                Q(is_private=True) | Q(is_organization_private=True)
            )
        count = queryset.update(is_visible=True)
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

    def rate_all_view(self, request):
        if not request.user.has_perm("judge.contest_rating"):
            raise PermissionDenied()
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE TABLE `%s`" % Rating._meta.db_table)
            Profile.objects.update(rating=None)
            for contest in Contest.objects.filter(
                is_rated=True, end_time__lte=timezone.now()
            ).order_by("end_time"):
                rate_contest(contest)
        return HttpResponseRedirect(reverse("admin:judge_contest_changelist"))

    def rate_view(self, request, id):
        if not request.user.has_perm("judge.contest_rating"):
            raise PermissionDenied()
        contest = get_object_or_404(Contest, id=id)
        if not contest.is_rated or not contest.ended:
            raise Http404()
        with transaction.atomic():
            contest.rate()
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

    def save_model(self, request, obj, form, change):
        super(ContestsSummaryAdmin, self).save_model(request, obj, form, change)
        obj.refresh_from_db()
        obj.results = recalculate_contest_summary_result(request, obj)
        obj.save()


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
    actions = None  # no bulk approve/reject — go through admin change_view

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("contest", "requested_by__user", "reviewed_by__user")
        )

    def changelist_view(self, request, extra_context=None):
        """Pre-compute per-row latest review verdict so the list page is N+1-safe.

        Stored on the request object (not a class attr) so concurrent requests
        don't share state. `review_verdict` then reads from this dict.
        """
        from judge.models.contest_review import (
            ContestReviewCheckResult,
            ContestReviewRun,
        )

        # The base changelist queryset will be paginated; recompute the slice
        # here would require parsing pagination params, so we materialize the
        # full filtered queryset's contest ids in one go. For realistic
        # request volumes (<100s pending) that's fine.
        qs = self.get_queryset(request)
        contest_ids = list(qs.values_list("contest_id", flat=True))

        latest_runs = {}
        for r in ContestReviewRun.objects.filter(
            contest_id__in=contest_ids, superseded_by__isnull=True
        ).order_by("-started_at"):
            latest_runs.setdefault(r.contest_id, r)

        done_run_ids = [
            r.id for r in latest_runs.values() if r.status == ContestReviewRun.DONE
        ]
        # Pull fail + error + warning counts so the verdict label is informative.
        fail_run_ids = set(
            ContestReviewCheckResult.objects.filter(
                run_id__in=done_run_ids,
                status__in=(
                    ContestReviewCheckResult.FAIL,
                    ContestReviewCheckResult.ERROR,
                ),
            )
            .values_list("run_id", flat=True)
            .distinct()
        )
        warn_run_ids = set(
            ContestReviewCheckResult.objects.filter(
                run_id__in=done_run_ids,
                status=ContestReviewCheckResult.WARNING,
            )
            .values_list("run_id", flat=True)
            .distinct()
        )

        # Stash on a thread-local — NOT on self. Each Django request runs in
        # its own thread (under WSGI/runserver), so concurrent admin requests
        # get independent storage. Storing on `self` would race: admin user A
        # and admin user B hitting the changelist simultaneously could see
        # each other's verdict caches because Django instantiates ModelAdmin
        # once per process.
        _admin_threadlocal.latest = latest_runs
        _admin_threadlocal.fail_ids = fail_run_ids
        _admin_threadlocal.warn_ids = warn_run_ids

        try:
            return super().changelist_view(request, extra_context)
        finally:
            # Clean up so the next request on the same thread starts fresh
            # (defensive — values would be overwritten anyway, but explicit
            # cleanup avoids leaking refs to large querysets to the next
            # request's tracebacks).
            for attr in ("latest", "fail_ids", "warn_ids"):
                if hasattr(_admin_threadlocal, attr):
                    delattr(_admin_threadlocal, attr)

    def review_verdict(self, obj):
        """Render the latest review verdict for this contest.

        Reads from the thread-local cache set in changelist_view (above).
        Falls back to "—" if no run exists or the cache isn't populated
        (e.g. when called from change_view, where bulk pre-fetch didn't run).
        """
        latest_runs = getattr(_admin_threadlocal, "latest", None) or {}
        latest = latest_runs.get(obj.contest_id)
        if latest is None:
            return "—"
        if latest.status == "R":
            return "Running"
        if latest.status == "E":
            return "Error"
        fail_ids = getattr(_admin_threadlocal, "fail_ids", set())
        warn_ids = getattr(_admin_threadlocal, "warn_ids", set())
        if latest.id in fail_ids:
            return "Fail"
        if latest.id in warn_ids:
            return "Warn"
        return "Pass"

    review_verdict.short_description = "Auto-review"

    def save_model(self, request, obj, form, change):
        """On approve/reject: post a system comment + (for approve) flip
        Contest.is_visible to True so the contest goes public, then notify
        the requesting author + admins.
        """
        from judge.models import ContestPublicRequest
        from judge.models.notification import Notification, NotificationCategory
        from judge.review.system_bot import post_system_comment_on_contest_review

        # Detect status transition by reading the old row before super().save().
        # On a brand-new row there's no transition to act on.
        old_status = None
        if change and obj.pk:
            try:
                old_status = ContestPublicRequest.objects.get(pk=obj.pk).status
            except ContestPublicRequest.DoesNotExist:
                pass

        if obj.status in (ContestPublicRequest.APPROVED, ContestPublicRequest.REJECTED):
            obj.reviewed_by = request.user.profile

        super().save_model(request, obj, form, change)

        if old_status == obj.status:
            # No transition (e.g. admin saved without changing status).
            return

        contest = obj.contest

        if obj.status == ContestPublicRequest.APPROVED:
            if not contest.is_visible:
                contest.is_visible = True
                contest.save(update_fields=["is_visible"])
            body = "**[System]** Admin approved this contest's public request."
            if obj.feedback:
                body += f"\n\n> {obj.feedback}"
            post_system_comment_on_contest_review(contest, body)

            if obj.requested_by:
                Notification.objects.create_notification(
                    owner=obj.requested_by,
                    category=NotificationCategory.CONTEST_PUBLIC_REQUEST_APPROVED,
                    html_link=f'<a href="/contest/{contest.key}/">{contest.name}</a>',
                    author=request.user.profile,
                )

        elif obj.status == ContestPublicRequest.REJECTED:
            body = "**[System]** Admin rejected this contest's public request."
            if obj.feedback:
                body += f"\n\n> {obj.feedback}"
            post_system_comment_on_contest_review(contest, body)

            if obj.requested_by:
                Notification.objects.create_notification(
                    owner=obj.requested_by,
                    category=NotificationCategory.CONTEST_PUBLIC_REQUEST_REJECTED,
                    html_link=(
                        f'<a href="/contest/{contest.key}/review/">{contest.name}</a>'
                    ),
                    author=request.user.profile,
                )


admin.site.register(ContestPublicRequest, ContestPublicRequestAdmin)
