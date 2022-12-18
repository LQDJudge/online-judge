from django.shortcuts import render
from django.utils.translation import gettext as _


def resolver(request):
    return render(
        request,
        "resolver/resolver.html",
        {
            "title": _("Resolver"),
        },
    )