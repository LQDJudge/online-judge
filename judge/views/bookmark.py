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
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from django.views.generic import View, ListView

from judge.models.bookmark import BookMark, MakeBookMark, dirty_bookmark

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
        bookmark = BookMark.objects.get(id=bookmark_id)
    except ValueError:
        return HttpResponseBadRequest()
    except BookMark.DoesNotExist:
        raise Http404()

    if delta == 0:
        bookmarklist = MakeBookMark.objects.filter(
            bookmark=bookmark, user=request.profile
        )
        if not bookmarklist.exists():
            newbookmark = MakeBookMark(
                bookmark=bookmark,
                user=request.profile,
            )
            newbookmark.save()
    else:
        bookmarklist = MakeBookMark.objects.filter(
            bookmark=bookmark, user=request.profile
        )
        if bookmarklist.exists():
            bookmarklist.delete()

    dirty_bookmark(bookmark, request.profile)

    return HttpResponse("success", content_type="text/plain")


def dobookmark_page(request):
    return bookmark_page(request, 0)


def undobookmark_page(request):
    return bookmark_page(request, 1)


class BookMarkDetailView(TemplateResponseMixin, SingleObjectMixin, View):
    def get_context_data(self, **kwargs):
        context = super(BookMarkDetailView, self).get_context_data(**kwargs)
        context["bookmark"] = self.object.get_or_create_bookmark()
        return context
