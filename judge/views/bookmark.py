from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
)
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from django.views.generic import View

from judge.models.bookmark import BookMark

__all__ = [
    "dobookmark_page",
    "undobookmark_page",
    "BookMarkDetailView",
]


@login_required
def bookmark_page(request, add=True):
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

    if add:  # Add bookmark
        bookmark.add_bookmark(request.profile)
    else:  # Remove bookmark
        bookmark.remove_bookmark(request.profile)

    return HttpResponse("success", content_type="text/plain")


def dobookmark_page(request):
    return bookmark_page(request, True)


def undobookmark_page(request):
    return bookmark_page(request, False)


class BookMarkDetailView(TemplateResponseMixin, SingleObjectMixin, View):
    def get_context_data(self, **kwargs):
        context = super(BookMarkDetailView, self).get_context_data(**kwargs)
        context["bookmark"] = self.object.get_or_create_bookmark()
        return context
