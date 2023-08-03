from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import F
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
)
from django.utils.translation import gettext as _
from judge.models.bookmark import BookMark, MakeBookMark
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from judge.dblock import LockModel
from django.views.generic import View, ListView


__all__ = [
    "dobookmark_page",
    "undobookmark_page",
    "BookMarkDetailView",
]


@login_required
def bookmark_page(request, delta):
    if request.method != "POST":
        return HttpResponseForbidden()

    if "id" not in request.POST:
        return HttpResponseBadRequest()

    try:
        bookmark_id = int(request.POST["id"])
        bookmark_page = BookMark.objects.filter(id=bookmark_id)
    except ValueError:
        return HttpResponseBadRequest()
    else:
        if not bookmark_page.exists():
            raise Http404()

    if delta == 0:
        bookmarklist = MakeBookMark.objects.filter(
            bookmark=bookmark_page.first(), user=request.profile
        )
        if not bookmarklist.exists():
            newbookmark = MakeBookMark(
                bookmark=bookmark_page.first(),
                user=request.profile,
            )
            newbookmark.save()
    else:
        bookmarklist = MakeBookMark.objects.filter(
            bookmark=bookmark_page.first(), user=request.profile
        )
        if bookmarklist.exists():
            bookmarklist.delete()

    return HttpResponse("success", content_type="text/plain")


def dobookmark_page(request):
    return bookmark_page(request, 0)


def undobookmark_page(request):
    return bookmark_page(request, 1)


class BookMarkDetailView(TemplateResponseMixin, SingleObjectMixin, View):
    def get_context_data(self, **kwargs):
        context = super(BookMarkDetailView, self).get_context_data(**kwargs)
        queryset = self.object.bookmark
        context["bookmark"] = queryset.first()
        return context
