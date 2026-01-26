import json

from django.conf import settings
from django.contrib.auth.context_processors import PermWrapper
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import Http404
from django.views.generic import DetailView

from reversion.models import Version

from judge.views.comment.mixins import CommentMixin, is_comment_locked


class CommentContent(CommentMixin, DetailView):
    template_name = "comments/content.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["vote_hide_threshold"] = settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD
        context["profile"] = self.request.profile
        context["perms"] = PermWrapper(self.request.user)
        context["comment_lock"] = is_comment_locked(self.request)
        return context


class CommentRevisionAjax(CommentMixin, DetailView):
    template_name = "comments/revision-ajax.html"

    def get_context_data(self, **kwargs):
        context = super(CommentRevisionAjax, self).get_context_data(**kwargs)
        revisions = Version.objects.get_for_object(self.object).order_by("-revision")

        if len(revisions) == 0:
            raise Http404

        try:
            wanted = min(
                max(int(self.request.GET.get("revision", 0)), 0), len(revisions) - 1
            )
            revision = revisions[wanted]
        except (ValueError, IndexError):
            raise Http404

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


class CommentVotesAjax(PermissionRequiredMixin, CommentMixin, DetailView):
    template_name = "comments/votes.html"
    permission_required = "judge.change_commentvote"

    def get_context_data(self, **kwargs):
        context = super(CommentVotesAjax, self).get_context_data(**kwargs)
        context["votes"] = self.object.votes.select_related("voter__user").only(
            "id", "voter__display_rank", "voter__user__username", "score"
        )
        return context
