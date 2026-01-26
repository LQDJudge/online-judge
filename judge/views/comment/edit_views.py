from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.utils.translation import gettext as _
from django.views.generic import UpdateView

from reversion import revisions

from judge.utils.views import TitleMixin
from judge.views.comment.forms import CommentEditForm
from judge.views.comment.mixins import CommentMixin
from judge.views.comment.utils import add_mention_notifications


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
