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
from judge.models.pagevote import PageVote, PageVoteVoter
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from judge.dblock import LockModel
from django.views.generic import View, ListView


__all__ = [
    "upvote_page",
    "downvote_page",
    "PageVoteDetailView",
    "PageVoteListView",
]


@login_required
def vote_page(request, delta):
    if abs(delta) != 1:
        return HttpResponseBadRequest(
            _("Messing around, are we?"), content_type="text/plain"
        )

    if request.method != "POST":
        return HttpResponseForbidden()

    if "id" not in request.POST:
        return HttpResponseBadRequest()

    if (
        not request.user.is_staff
        and not request.profile.submission_set.filter(
            points=F("problem__points")
        ).exists()
    ):
        return HttpResponseBadRequest(
            _("You must solve at least one problem before you can vote."),
            content_type="text/plain",
        )

    try:
        pagevote_id = int(request.POST["id"])
    except ValueError:
        return HttpResponseBadRequest()
    else:
        if not PageVote.objects.filter(id=pagevote_id).exists():
            raise Http404()

    vote = PageVoteVoter()
    vote.pagevote_id = pagevote_id
    vote.voter = request.profile
    vote.score = delta

    while True:
        try:
            vote.save()
        except IntegrityError:
            with LockModel(write=(PageVoteVoter,)):
                try:
                    vote = PageVoteVoter.objects.get(
                        pagevote_id=pagevote_id, voter=request.profile
                    )
                except PageVoteVoter.DoesNotExist:
                    # We must continue racing in case this is exploited to manipulate votes.
                    continue
                vote.delete()
            PageVote.objects.filter(id=pagevote_id).update(
                score=F("score") - vote.score
            )
        else:
            PageVote.objects.filter(id=pagevote_id).update(score=F("score") + delta)
        break
    return HttpResponse("success", content_type="text/plain")


def upvote_page(request):
    return vote_page(request, 1)


def downvote_page(request):
    return vote_page(request, -1)


class PageVoteDetailView(TemplateResponseMixin, SingleObjectMixin, View):
    pagevote_page = None

    def get_pagevote_page(self):
        if self.pagevote_page is None:
            raise NotImplementedError()
        return self.pagevote_page

    def get_context_data(self, **kwargs):
        context = super(PageVoteDetailView, self).get_context_data(**kwargs)
        queryset = PageVote.objects.get_or_create(page=self.get_comment_page())
        context["pagevote"] = queryset[0]
        return context


class PageVoteListView:
    def add_pagevote_context_data(self, context, obj_list="object_list"):
        for item in context[obj_list]:
            pagevote, _ = PageVote.objects.get_or_create(
                page=self.get_comment_page(item)
            )
            setattr(item, "pagevote", pagevote)
        return context
