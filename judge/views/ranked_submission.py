from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _

from judge.models import Language, Submission
from judge.utils.problems import get_result_data
from judge.utils.raw_sql import join_sql_subquery
from judge.views.submission import ForceContestMixin, ProblemSubmissions
from judge.utils.infinite_paginator import InfinitePaginationMixin

__all__ = ["RankedSubmissions", "ContestRankedSubmission"]


class RankedSubmissions(InfinitePaginationMixin, ProblemSubmissions):
    page_type = "best_submissions_list"
    dynamic_update = False

    def get_queryset(self):
        queryset = super(RankedSubmissions, self).get_queryset()

        if self.in_contest:
            return queryset.order_by("-contest__points", "time")
        else:
            return queryset.order_by("-case_points", "time")

    def get_title(self):
        return _("Best solutions for %s") % self.problem_name

    def get_content_title(self):
        return mark_safe(
            escape(_("Best solutions for %s"))
            % (
                format_html(
                    '<a href="{1}">{0}</a>',
                    self.problem_name,
                    reverse("problem_detail", args=[self.problem.code]),
                ),
            )
        )

    def _get_result_data(self, queryset=None):
        if queryset is None:
            queryset = super(RankedSubmissions, self).get_queryset()
        return get_result_data(queryset.order_by())


class ContestRankedSubmission(ForceContestMixin, RankedSubmissions):
    def get_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return _("Best solutions for %(problem)s in %(contest)s") % {
                "problem": self.problem_name,
                "contest": self.contest.name,
            }
        return _("Best solutions for problem %(number)s in %(contest)s") % {
            "number": self.get_problem_number(self.problem),
            "contest": self.contest.name,
        }

    def get_content_title(self):
        if self.problem.is_accessible_by(self.request.user):
            return mark_safe(
                escape(_("Best solutions for %(problem)s in %(contest)s"))
                % {
                    "problem": format_html(
                        '<a href="{1}">{0}</a>',
                        self.problem_name,
                        reverse("problem_detail", args=[self.problem.code]),
                    ),
                    "contest": format_html(
                        '<a href="{1}">{0}</a>',
                        self.contest.name,
                        reverse("contest_view", args=[self.contest.key]),
                    ),
                }
            )
        return mark_safe(
            escape(_("Best solutions for problem %(number)s in %(contest)s"))
            % {
                "number": self.get_problem_number(self.problem),
                "contest": format_html(
                    '<a href="{1}">{0}</a>',
                    self.contest.name,
                    reverse("contest_view", args=[self.contest.key]),
                ),
            }
        )

    def _get_queryset(self):
        return super()._get_queryset().filter(contest_object=self.contest)
