from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy as _, ungettext

from judge.models import VolunteerProblemVote

class VolunteerProblemVoteAdmin(admin.ModelAdmin):
    fields = ('voter', 'problem', 'time', 'thinking_points', 'knowledge_points', 'feedback')
    readonly_fields = ('time', 'problem', 'voter')
    list_display = ('voter', 'problem_link', 'time', 'thinking_points', 'knowledge_points', 'feedback')
    date_hierarchy = 'time'

    def problem_link(self, obj):
        url = reverse('admin:judge_problem_change', args=(obj.problem.id,))
        return format_html(f"<a href='{url}'>{obj.problem.code}</a>")
    problem_link.short_description = _('Problem')
    problem_link.admin_order_field = 'problem__code'