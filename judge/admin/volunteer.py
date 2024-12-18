from operator import attrgetter

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy as _, ngettext
from django.forms import ModelForm

from judge.models import VolunteerProblemVote
from judge.widgets import AdminSelect2MultipleWidget


class VolunteerProblemVoteForm(ModelForm):
    class Meta:
        widgets = {
            "types": AdminSelect2MultipleWidget,
        }


class VolunteerProblemVoteAdmin(admin.ModelAdmin):
    form = VolunteerProblemVoteForm
    fields = (
        "voter",
        "problem_link",
        "time",
        "thinking_points",
        "knowledge_points",
        "types",
        "feedback",
    )
    readonly_fields = ("time", "problem_link", "voter")
    list_display = (
        "voter",
        "problem_link",
        "thinking_points",
        "knowledge_points",
        "show_types",
        "feedback",
    )
    search_fields = (
        "voter__user__username",
        "problem__code",
        "problem__name",
    )
    date_hierarchy = "time"

    def problem_link(self, obj):
        if self.request.user.is_superuser:
            url = reverse("admin:judge_problem_change", args=(obj.problem.id,))
        else:
            url = reverse("problem_detail", args=(obj.problem.code,))
        return format_html(f"<a href='{url}'>{obj.problem}</a>")

    problem_link.short_description = _("Problem")
    problem_link.admin_order_field = "problem__code"

    def show_types(self, obj):
        return ", ".join(map(attrgetter("name"), obj.types.all()))

    show_types.short_description = _("Types")

    def get_queryset(self, request):
        self.request = request
        if request.user.is_superuser:
            return super().get_queryset(request)
        queryset = VolunteerProblemVote.objects.prefetch_related("voter")
        return queryset.filter(voter=request.profile).distinct()
