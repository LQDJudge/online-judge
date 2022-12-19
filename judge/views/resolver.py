from django.shortcuts import render
from django.utils.translation import gettext as _
from django.http import HttpResponseForbidden

def resolver(request):
    if request.user.is_superuser:
        return render(
            request,
            "resolver/resolver.html",
            {
                "title": _("Resolver"),
            },
        )
    return HttpResponseForbidden()
    