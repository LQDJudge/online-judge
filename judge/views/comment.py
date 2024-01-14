from django.conf import settings

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.context_processors import PermWrapper
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import Q, F, Count, FilteredRelation
from django.db.models.functions import Coalesce
from django.db.models.expressions import F, Value
from django.forms.models import ModelForm
from django.http import (
    Http404,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
)
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, UpdateView
from django.urls import reverse_lazy
from reversion import revisions
from reversion.models import Version
from django.conf import settings
from django_ratelimit.decorators import ratelimit

from judge.models import Comment, CommentVote, Notification, BlogPost
from judge.utils.views import TitleMixin
from judge.widgets import MathJaxPagedownWidget, HeavyPreviewPageDownWidget
from judge.comments import add_mention_notifications

import json

__all__ = [
    "upvote_comment",
    "downvote_comment",
    "CommentEditAjax",
    "CommentContent",
    "CommentEdit",
]


@ratelimit(key="user", rate=settings.RL_VOTE)
@login_required
def vote_comment(request, delta):
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
        comment_id = int(request.POST["id"])
    except ValueError:
        return HttpResponseBadRequest()
    else:
        if not Comment.objects.filter(id=comment_id).exists():
            raise Http404()

    vote = CommentVote()
    vote.comment_id = comment_id
    vote.voter = request.profile
    vote.score = delta

    try:
        vote.save()
    except IntegrityError:
        try:
            vote = CommentVote.objects.get(comment_id=comment_id, voter=request.profile)
        except CommentVote.DoesNotExist:
            raise Http404()
        if -vote.score != delta:
            return HttpResponseBadRequest(
                _("You already voted."), content_type="text/plain"
            )
        vote.delete()
        Comment.objects.filter(id=comment_id).update(score=F("score") - vote.score)
    else:
        Comment.objects.filter(id=comment_id).update(score=F("score") + delta)
    return HttpResponse("success", content_type="text/plain")


def upvote_comment(request):
    return vote_comment(request, 1)


def downvote_comment(request):
    return vote_comment(request, -1)


def get_comments(request, limit=10):
    try:
        comment_id = int(request.GET["id"])
        parent_none = int(request.GET["parent_none"])
    except ValueError:
        return HttpResponseBadRequest()
    else:
        if comment_id and not Comment.objects.filter(id=comment_id).exists():
            raise Http404()

    offset = 0
    if "offset" in request.GET:
        offset = int(request.GET["offset"])

    target_comment = -1
    if "target_comment" in request.GET:
        target_comment = int(request.GET["target_comment"])

    comment_root_id = 0

    if comment_id:
        comment_obj = Comment.objects.get(pk=comment_id)
        comment_root_id = comment_obj.id
    else:
        comment_obj = None

    queryset = comment_obj.linked_object.comments
    if parent_none:
        queryset = queryset.filter(parent=None, hidden=False)
        queryset = queryset.exclude(pk=target_comment)
    else:
        queryset = queryset.filter(parent=comment_obj, hidden=False)
    comment_count = len(queryset)
    queryset = (
        queryset.select_related("author__user")
        .defer("author__about")
        .annotate(
            count_replies=Count("replies", distinct=True),
        )[offset : offset + limit]
    )
    profile = None
    if request.user.is_authenticated:
        profile = request.profile
        queryset = queryset.annotate(
            my_vote=FilteredRelation("votes", condition=Q(votes__voter_id=profile.id)),
        ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))

    new_offset = offset + min(len(queryset), limit)

    return render(
        request,
        "comments/content-list.html",
        {
            "profile": profile,
            "comment_root_id": comment_root_id,
            "comment_list": queryset,
            "vote_hide_threshold": settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD,
            "perms": PermWrapper(request.user),
            "offset": new_offset,
            "limit": limit,
            "comment_count": comment_count,
            "comment_parent_none": parent_none,
            "target_comment": target_comment,
            "comment_more": comment_count - new_offset,
        },
    )


def get_show_more(request):
    return get_comments(request)


def get_replies(request):
    return get_comments(request)


class CommentMixin(object):
    model = Comment
    pk_url_kwarg = "id"
    context_object_name = "comment"


class CommentRevisionAjax(CommentMixin, DetailView):
    template_name = "comments/revision-ajax.html"

    def get_context_data(self, **kwargs):
        context = super(CommentRevisionAjax, self).get_context_data(**kwargs)
        revisions = Version.objects.get_for_object(self.object).order_by("-revision")
        try:
            wanted = min(
                max(int(self.request.GET.get("revision", 0)), 0), len(revisions) - 1
            )
        except ValueError:
            raise Http404
        revision = revisions[wanted]
        data = json.loads(revision.serialized_data)
        try:
            context["body"] = data[0]["fields"]["body"]
        except Exception:
            context["body"] = ""
        return context

    def get_object(self, queryset=None):
        comment = super(CommentRevisionAjax, self).get_object(queryset)
        if comment.hidden and not self.request.user.has_perm("judge.change_comment"):
            raise Http404()
        return comment


class CommentEditForm(ModelForm):
    class Meta:
        model = Comment
        fields = ["body"]
        widgets = {
            "body": HeavyPreviewPageDownWidget(
                id="id-edit-comment-body",
                preview=reverse_lazy("comment_preview"),
                preview_timeout=1000,
                hide_preview_button=True,
            ),
        }


class CommentEditAjax(LoginRequiredMixin, CommentMixin, UpdateView):
    template_name = "comments/edit-ajax.html"
    form_class = CommentEditForm

    def form_valid(self, form):
        # update notifications
        comment = form.instance
        add_mention_notifications(comment)
        comment.revision_count = comment.versions.count() + 1
        comment.save(update_fields=["revision_count"])
        with revisions.create_revision():
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            return super(CommentEditAjax, self).form_valid(form)

    def get_success_url(self):
        return self.object.get_absolute_url()

    def get_object(self, queryset=None):
        comment = super(CommentEditAjax, self).get_object(queryset)
        if self.request.user.has_perm("judge.change_comment"):
            return comment
        profile = self.request.profile
        if profile != comment.author or profile.mute or comment.hidden:
            raise Http404()
        return comment


class CommentEdit(TitleMixin, CommentEditAjax):
    template_name = "comments/edit.html"

    def get_title(self):
        return _("Editing comment")


class CommentContent(CommentMixin, DetailView):
    template_name = "comments/content.html"


class CommentVotesAjax(PermissionRequiredMixin, CommentMixin, DetailView):
    template_name = "comments/votes.html"
    permission_required = "judge.change_commentvote"

    def get_context_data(self, **kwargs):
        context = super(CommentVotesAjax, self).get_context_data(**kwargs)
        context["votes"] = self.object.votes.select_related("voter__user").only(
            "id", "voter__display_rank", "voter__user__username", "score"
        )
        return context


@require_POST
def comment_hide(request):
    if not request.user.has_perm("judge.change_comment"):
        raise PermissionDenied()
    try:
        comment_id = int(request.POST["id"])
    except ValueError:
        return HttpResponseBadRequest()

    comment = get_object_or_404(Comment, id=comment_id)
    comment.get_descendants(include_self=True).update(hidden=True)
    return HttpResponse("ok")
