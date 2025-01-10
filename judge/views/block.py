from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
)
from django.utils.translation import gettext as _
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from django.views.generic import View

from django.contrib.contenttypes.models import ContentType
from judge.models import Block

__all__ = [
    "add_block",
    "remove_block",
    "BlockDetailView",
]


@login_required
def block_page(request, add=True):
    if request.method != "POST":
        return HttpResponseForbidden()

    # Validate input fields
    required_fields = ["blocker_type", "blocker_id", "blocked_type", "blocked_id"]
    if not all(field in request.POST for field in required_fields):
        return HttpResponseBadRequest("Missing required fields.")

    try:
        # Extract and parse input data
        blocker_type = request.POST["blocker_type"]
        blocker_id = int(request.POST["blocker_id"])
        blocked_type = request.POST["blocked_type"]
        blocked_id = int(request.POST["blocked_id"])

        # Resolve content types and objects
        blocker_ct = ContentType.objects.get(model=blocker_type.lower())
        blocked_ct = ContentType.objects.get(model=blocked_type.lower())

        blocker = blocker_ct.get_object_for_this_type(id=blocker_id)
        blocked = blocked_ct.get_object_for_this_type(id=blocked_id)
    except ContentType.DoesNotExist:
        return HttpResponseBadRequest("Invalid content type.")
    except ValueError:
        return HttpResponseBadRequest("Invalid blocker or blocked ID.")
    except Exception as e:
        return HttpResponseBadRequest(f"An error occurred: {e}")

    if add:  # Add block
        Block.add_block(blocker, blocked)
    else:  # Remove block
        Block.remove_block(blocker, blocked)

    return HttpResponse("success", content_type="text/plain")


def add_block(request):
    return block_page(request, add=True)


def remove_block(request):
    return block_page(request, add=False)


class BlockDetailView(TemplateResponseMixin, SingleObjectMixin, View):
    def get_context_data(self, **kwargs):
        context = super(BlockDetailView, self).get_context_data(**kwargs)
        context["block"] = self.object
        return context
