import traceback

from django.shortcuts import render
from django.utils.translation import gettext as _


def error(request, context, status):
    return render(request, "error.html", context=context, status=status)


def error404(request, exception=None):
    # TODO: "panic: go back"
    return render(
        request,
        "generic-message.html",
        {
            "title": _("Page not available"),
            "message": _(
                "The page \"%s\" doesn't exist or you don't have permission to"
                " view it."
            )
            % request.path,
        },
        status=404,
    )


def error403(request, exception=None):
    # Prefer the specific message the view raised, e.g.
    # PermissionDenied(_("You do not have permission to edit this quiz.")).
    message = str(exception) if exception is not None else ""
    if not message:
        message = _('You don\'t have permission to access "%s".') % request.path
    return render(
        request,
        "generic-message.html",
        {
            "title": _("Access denied"),
            "message": message,
        },
        status=403,
    )


def error500(request):
    return error(
        request,
        {
            "id": "invalid_state",
            "description": _("corrupt page %s") % request.path,
            "traceback": traceback.format_exc(),
            "code": 500,
        },
        500,
    )
