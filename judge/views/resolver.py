from django.views.generic import TemplateView
from django.utils.translation import gettext as _
from django.http import HttpResponseForbidden


class Resolver(TemplateView):
    title = _("Resolver")
    template_name = "resolver/resolver.html"

    def get_context_data(self, **kwargs):
        context = super(Resolver, self).get_context_data(**kwargs)
        context["contest_json"] = "/static/contest.json"
        return context

    def get(self, request, *args, **kwargs):
        if request.user.is_superuser:
            return super(Resolver, self).get(request, *args, **kwargs)
        return HttpResponseForbidden()
