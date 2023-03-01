from django.forms import ModelForm
from django.urls import reverse_lazy
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _, ungettext
from reversion.admin import VersionAdmin

from judge.models import Comment
from judge.widgets import AdminHeavySelect2Widget, HeavyPreviewAdminPageDownWidget


class CommentForm(ModelForm):
    class Meta:
        widgets = {
            "author": AdminHeavySelect2Widget(data_view="profile_select2"),
            "parent": AdminHeavySelect2Widget(data_view="comment_select2"),
        }
        if HeavyPreviewAdminPageDownWidget is not None:
            widgets["body"] = HeavyPreviewAdminPageDownWidget(
                preview=reverse_lazy("comment_preview")
            )


class CommentAdmin(VersionAdmin):
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "author",
                    "parent",
                    "score",
                    "hidden",
                    "content_type",
                    "object_id",
                )
            },
        ),
        ("Content", {"fields": ("body",)}),
    )
    list_display = ["author", "linked_object", "time"]
    search_fields = ["author__user__username", "body"]
    readonly_fields = ["score"]
    actions = ["hide_comment", "unhide_comment"]
    list_filter = ["hidden"]
    actions_on_top = True
    actions_on_bottom = True
    form = CommentForm
    date_hierarchy = "time"

    def get_queryset(self, request):
        return Comment.objects.order_by("-time")

    def hide_comment(self, request, queryset):
        count = queryset.update(hidden=True)
        self.message_user(
            request,
            ungettext(
                "%d comment successfully hidden.",
                "%d comments successfully hidden.",
                count,
            )
            % count,
        )

    hide_comment.short_description = _("Hide comments")

    def unhide_comment(self, request, queryset):
        count = queryset.update(hidden=False)
        self.message_user(
            request,
            ungettext(
                "%d comment successfully unhidden.",
                "%d comments successfully unhidden.",
                count,
            )
            % count,
        )

    unhide_comment.short_description = _("Unhide comments")

    def save_model(self, request, obj, form, change):
        super(CommentAdmin, self).save_model(request, obj, form, change)
        if obj.hidden:
            obj.get_descendants().update(hidden=obj.hidden)
