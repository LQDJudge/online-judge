from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import Http404
from django.utils.translation import gettext as _
from django.views.generic import UpdateView

from reversion import revisions

from judge.models import Comment, CommentModerationLog
from judge.utils.views import TitleMixin
from judge.tasks.omni_moderation import enqueue_omni, record_omni_pending
from judge.views.comment.forms import CommentEditForm
from judge.views.comment.mixins import CommentMixin
from judge.views.comment.utils import add_mention_notifications


class CommentEditAjax(LoginRequiredMixin, CommentMixin, UpdateView):
    template_name = "comments/edit-ajax.html"
    form_class = CommentEditForm

    @transaction.atomic
    def form_valid(self, form):
        # update notifications
        comment = form.instance
        # Use the same lock order as moderation when an edit supersedes a check.
        list(
            CommentModerationLog.objects.select_for_update()
            .filter(comment_id=comment.pk)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        # Moderation may have hidden the comment after get_object() ran.
        current = Comment.objects.select_for_update().filter(pk=comment.pk).first()
        if current is None or current.hidden:
            raise Http404()
        add_mention_notifications(comment)
        comment.revision_count = comment.versions.count() + 1
        comment.save(update_fields=["revision_count"])
        with revisions.create_revision():
            revisions.set_comment(_("Edited from site"))
            revisions.set_user(self.request.user)
            response = super(CommentEditAjax, self).form_valid(form)
        if record_omni_pending("comment", self.object):
            transaction.on_commit(
                lambda: enqueue_omni("comment", self.object.pk, self.object.body)
            )
        return response

    def get_success_url(self):
        return self.object.get_absolute_url()

    def get_object(self, queryset=None):
        comment = super(CommentEditAjax, self).get_object(queryset)
        profile = self.request.profile
        if profile != comment.author or profile.mute or comment.hidden:
            raise Http404()
        return comment


class CommentEdit(TitleMixin, CommentEditAjax):
    template_name = "comments/edit.html"

    def get_title(self):
        return _("Editing comment")
