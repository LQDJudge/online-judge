from django.views.generic import ListView
from django.utils.translation import gettext as _, gettext_lazy
from django.db.models import Count
from django.http import HttpResponseForbidden

from judge.utils.diggpaginator import DiggPaginator
from judge.models import VolunteerProblemVote, Problem


class InternalProblem(ListView):
    model = Problem
    title = _("Internal problems")
    template_name = "internal/base.html"
    paginate_by = 100
    context_object_name = "problems"

    def get_paginator(
        self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs
    ):
        return DiggPaginator(
            queryset,
            per_page,
            body=6,
            padding=2,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
            **kwargs
        )

    def get_queryset(self):
        queryset = (
            Problem.objects.annotate(vote_count=Count("volunteer_user_votes"))
            .filter(vote_count__gte=1)
            .order_by("-vote_count")
        )
        return queryset

    def get_context_data(self, **kwargs):
        context = super(InternalProblem, self).get_context_data(**kwargs)
        context["page_type"] = "problem"
        context["title"] = self.title
        return context

    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return super(InternalProblem, self).get(request, *args, **kwargs)
        return HttpResponseForbidden()
