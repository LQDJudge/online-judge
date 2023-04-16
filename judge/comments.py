from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Count, FilteredRelation, Q
from django.db.models.expressions import F, Value
from django.db.models.functions import Coalesce
from django.forms import ModelForm
from django.http import (
    HttpResponseForbidden,
    HttpResponseNotFound,
    HttpResponseRedirect,
)
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.generic import View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin
from reversion import revisions
from reversion.models import Revision, Version

from judge.dblock import LockModel
from judge.models import Comment, Notification
from judge.widgets import HeavyPreviewPageDownWidget
from judge.jinja2.reference import get_user_from_text


def add_mention_notifications(comment):
    user_referred = get_user_from_text(comment.body).exclude(id=comment.author.id)
    for user in user_referred:
        notification_ref = Notification(owner=user, comment=comment, category="Mention")
        notification_ref.save()


def del_mention_notifications(comment):
    query = {"comment": comment, "category": "Mention"}
    Notification.objects.filter(**query).delete()


class CommentForm(ModelForm):
    class Meta:
        model = Comment
        fields = ["body", "parent"]
        widgets = {
            "parent": forms.HiddenInput(),
        }

        if HeavyPreviewPageDownWidget is not None:
            widgets["body"] = HeavyPreviewPageDownWidget(
                preview=reverse_lazy("comment_preview"),
                preview_timeout=1000,
                hide_preview_button=True,
            )

    def __init__(self, request, *args, **kwargs):
        self.request = request
        super(CommentForm, self).__init__(*args, **kwargs)
        self.fields["body"].widget.attrs.update({"placeholder": _("Comment body")})

    def clean(self):
        if self.request is not None and self.request.user.is_authenticated:
            profile = self.request.profile
            if profile.mute:
                raise ValidationError(_("Your part is silent, little toad."))
            elif (
                not self.request.user.is_staff
                and not profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            ):
                raise ValidationError(
                    _(
                        "You need to have solved at least one problem "
                        "before your voice can be heard."
                    )
                )
        return super(CommentForm, self).clean()


class CommentedDetailView(TemplateResponseMixin, SingleObjectMixin, View):
    comment_page = None

    def get_comment_page(self):
        if self.comment_page is None:
            raise NotImplementedError()
        return self.comment_page

    def is_comment_locked(self):
        if self.request.user.has_perm("judge.override_comment_lock"):
            return False
        return (
            self.request.in_contest
            and self.request.participation.contest.use_clarifications
        )

    @method_decorator(login_required)
    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if self.is_comment_locked():
            return HttpResponseForbidden()

        parent = request.POST.get("parent")
        if parent:
            try:
                parent = int(parent)
            except ValueError:
                return HttpResponseNotFound()
            else:
                if not self.object.comments.filter(hidden=False, id=parent).exists():
                    return HttpResponseNotFound()

        form = CommentForm(request, request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.author = request.profile
            comment.linked_object = self.object

            with LockModel(
                write=(Comment, Revision, Version), read=(ContentType,)
            ), revisions.create_revision():
                revisions.set_user(request.user)
                revisions.set_comment(_("Posted comment"))
                comment.save()

            # add notification for reply
            if comment.parent and comment.parent.author != comment.author:
                notification_reply = Notification(
                    owner=comment.parent.author, comment=comment, category="Reply"
                )
                notification_reply.save()

            # add notification for page authors
            page_authors = comment.linked_object.authors.all()
            for user in page_authors:
                if user == comment.author:
                    continue
                notification = Notification(
                    owner=user, comment=comment, category="Comment"
                )
                notification.save()
            # except Exception:
            #     pass

            add_mention_notifications(comment)

            return HttpResponseRedirect(comment.get_absolute_url())

        context = self.get_context_data(object=self.object, comment_form=form)
        return self.render_to_response(context)

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.render_to_response(
            self.get_context_data(
                object=self.object,
                comment_form=CommentForm(request, initial={"parent": None}),
            )
        )

    def get_context_data(self, **kwargs):
        context = super(CommentedDetailView, self).get_context_data(**kwargs)
        queryset = self.object.comments
        queryset = queryset.filter(parent=None, hidden=False)
        comment_count = len(queryset)
        queryset = (
            queryset.select_related("author__user")
            .defer("author__about")
            .annotate(
                count_replies=Count("replies", distinct=True), 
                revisions=Count("versions", distinct=True),
            )[:10]
        )
        context["has_comments"] = queryset.exists()
        context["comment_lock"] = self.is_comment_locked()
        if self.request.user.is_authenticated:
            profile = self.request.profile
            queryset = queryset.annotate(
                my_vote=FilteredRelation(
                    "votes", condition=Q(votes__voter_id=profile.id)
                ),
            ).annotate(vote_score=Coalesce(F("my_vote__score"), Value(0)))
            context["is_new_user"] = (
                not self.request.user.is_staff
                and not profile.submission_set.filter(
                    points=F("problem__points")
                ).exists()
            )
        context["comment_list"] = queryset
        context["vote_hide_threshold"] = settings.DMOJ_COMMENT_VOTE_HIDE_THRESHOLD
        context["comment_root_id"] = 0
        context["offset"] = 10
        context["limit"] = 10
        context["comment_count"] = comment_count
        return context
